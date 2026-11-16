"""Local, bounded raster editing. Originals and session ownership stay intact."""
import hashlib
import io
import base64
import math
import re
import threading
import warnings
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app import artifacts, media, ui_auth

router = APIRouter(prefix='/v9/artifacts')
MODEL_SHA256 = '309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8'
_gate = threading.BoundedSemaphore(1)
_session = None


def chat_images(text, sid):
    """Only explicitly attached pictures in this turn, bound to its session."""
    from PIL import Image
    result, tokens, total, seen = [], 0, 0, set()
    for fid, owner in re.findall(r'/v6/media/([a-f0-9]{32})\?session_id=([1-9]\d*)', text):
        if fid in seen:
            continue
        seen.add(fid)
        if int(owner) != sid:
            continue
        row, path = media.read(fid, sid)
        if row['mime'] not in ('image/png', 'image/jpeg', 'image/webp'):
            continue
        if len(result) >= 8 or total + row['size'] > 60 * 1024 * 1024:
            raise HTTPException(413, '每轮最多发送 8 张、合计 60 MiB 的图片')
        raw = path.read_bytes()
        if len(raw) != row['size'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise HTTPException(409, '图片已变化，请重新添加图片')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as image:
                    width, height = image.size
                    if image.format not in ('PNG', 'JPEG', 'WEBP') or image.n_frames != 1 or max(width, height) > 8192 or width * height > 16777216:
                        raise ValueError('image dimensions')
                    image.verify()
        except Exception as error:
            raise HTTPException(422, '此图片无法发送，请重新导出为 PNG 或 JPEG') from error
        tokens += 85 + 170 * math.ceil(width / 512) * math.ceil(height / 512)
        total += len(raw)
        result.append({'type': 'image_url', 'image_url': {'url': 'data:' + row['mime'] + ';base64,' + base64.b64encode(raw).decode('ascii')}})
    return result, tokens


def with_chat_images(messages, parts):
    if not parts:
        return messages
    result = list(messages)
    for index in range(len(result) - 1, -1, -1):
        if result[index]['role'] == 'user':
            result[index] = {**result[index], 'content': [{'type': 'text', 'text': str(result[index]['content'])}, *parts]}
            break
    return result


class RemoveBackground(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: int = Field(gt=0)
    expected_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def model_session():
    global _session
    if _session is None:
        import onnxruntime as ort
        from app import config
        model = config.BUNDLED_MODEL_ROOT / 'image/u2netp.onnx'
        if not model.is_file() or hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA256:
            raise HTTPException(503, '本地抠图模型缺失或损坏，请重新安装图片工具')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        _session = ort.InferenceSession(str(model), sess_options=options, providers=['CPUExecutionProvider'])
    return _session


def remove_background(raw):
    from PIL import Image, ImageChops, ImageOps
    import numpy as np
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in ('PNG', 'JPEG', 'WEBP') or getattr(source, 'n_frames', 1) != 1:
                    raise ValueError('只支持单帧 PNG、JPEG、WebP 图片')
                if source.width > 8192 or source.height > 8192 or source.width * source.height > 16777216:
                    raise ValueError('图片需在 8192 边长、1600 万像素以内')
                image = ImageOps.exif_transpose(source).convert('RGBA')
        except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
            raise HTTPException(422, '图片无法处理或尺寸过大') from error
    small = image.convert('RGB').resize((320, 320), Image.Resampling.LANCZOS)
    values = np.asarray(small, dtype=np.float32)
    values /= max(1.0, float(values.max()))
    values = (values - np.asarray([.485, .456, .406], dtype=np.float32)) / np.asarray([.229, .224, .225], dtype=np.float32)
    model = model_session()
    prediction = model.run(None, {model.get_inputs()[0].name: values.transpose(2, 0, 1)[None]})[0][0, 0]
    if not np.isfinite(prediction).all():
        raise HTTPException(422, '本次抠图未得到有效结果，原图已保留')
    low, high = float(prediction.min()), float(prediction.max())
    if high - low < 1e-6:
        raise HTTPException(422, '未识别到清晰主体，原图已保留')
    mask = Image.fromarray(np.uint8(np.clip((prediction - low) / (high - low), 0, 1) * 255)).resize(image.size, Image.Resampling.LANCZOS)
    image.putalpha(ImageChops.multiply(image.getchannel('A'), mask))
    output = io.BytesIO()
    image.save(output, format='PNG')
    if output.tell() > media.LIMIT:
        raise HTTPException(413, '处理后图片超过 15 MiB，请先缩小图片')
    return output.getvalue()


@router.post('/{fid}/remove-background')
def remove_background_artifact(fid: str, body: RemoveBackground, request: Request):
    ui_auth.sessions.principal(request)
    row, path = media.read(fid, body.session_id)
    if row['mime'] not in ('image/png', 'image/jpeg', 'image/webp'):
        raise HTTPException(422, '此附件不是可编辑图片')
    raw = path.read_bytes()
    if len(raw) != row['size'] or hashlib.sha256(raw).hexdigest() != body.expected_sha256:
        raise HTTPException(409, '图片已变化，请重新打开预览')
    if not _gate.acquire(blocking=False):
        raise HTTPException(409, '另一张图片正在处理中，请稍后重试')
    try:
        result = remove_background(raw)
        name = Path(row['name']).stem[:170] + '-去背景.png'
        return artifacts.store(result, 'image/png', body.session_id, name,
                               {'kind': 'image-edit', 'previous_id': fid, 'operation': 'remove-background', 'source_sha256': row['sha256']})
    finally:
        _gate.release()
