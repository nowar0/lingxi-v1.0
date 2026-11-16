"""Connection inventory only. Deferred channels do not open sockets or run adapters."""
from fastapi import APIRouter

router = APIRouter(prefix='/v11/connections', tags=['connections'])


@router.get('')
def listing():
    return {'items': [
        {'id': 'qq', 'name': 'QQ', 'description': '后续通过 AstrBot / NapCat 接入陪伴消息。', 'state': 'deferred'},
        {'id': 'android', 'name': '安卓客户端', 'description': '后续管理移动设备与配对。', 'state': 'deferred'},
        {'id': 'sync', 'name': '常在线与跨设备同步', 'description': '后续管理同步设备和常在线服务。', 'state': 'deferred'},
    ], 'automatic_connections': False}
