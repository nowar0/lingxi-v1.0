import type { ReasoningCapability } from './workComposerSettings'
import type { ModelProtocol } from '@/types'
export type Protocol = ModelProtocol
export type CatalogMode = 'service' | 'custom' | 'preset'
export interface CatalogModel {
  id: string; display_name?: string; capabilities: string[]
  input_modalities?: string[]; output_modalities?: string[]; capability_source?: string
  capability_status?: 'unverified' | 'untested'
  documentation?: string; reasoning?: ReasoningCapability & { parameter?: 'effort' | 'thinking' }
}
export interface StoredKey {
  id: string; name: string; base_url: string; revision: number; owned: boolean
  models: CatalogModel[]; connected: boolean | null; note: string; discovered_at: string
  provider_id?: string; protocol?: Protocol; catalog_mode?: CatalogMode; custom_models?: CatalogModel[]
}
export interface ProviderPreset { id: string; name: string; base_url: string; protocol: Protocol; documentation: string; models: CatalogModel[] }
export const modalityNames: Record<string, string> = { text: '文本', image: '图像', audio: '音频', video: '视频', document: '文件' }
export const nativeVideoInputLabel = '原生视频输入（需提供方支持；未实测）'
export const capabilityNames: Record<string, string> = { text: '文字', vision: '图像输入（视觉）', video: nativeVideoInputLabel, image: '图像生成（生图）', stt: '语音识别', tts: '语音合成' }
export const capabilitySourceNames: Record<string, string> = { manual: '用户声明', service: '服务声明', documentation: '文档建议', unknown: '来源未知' }
export function matchesCatalogModel(model: Pick<CatalogModel, 'id' | 'display_name'>, query: string) {
  const value = query.trim().toLocaleLowerCase()
  return !value || [model.id, model.display_name || ''].some(text => text.toLocaleLowerCase().includes(value))
}
export function configurationModels(models: CatalogModel[]) {
  return models.map((model) => ({ id: model.id.trim(), display_name: model.display_name || '', capabilities: model.capabilities,
    input_modalities: model.input_modalities || [], output_modalities: model.output_modalities || [],
    ...(model.reasoning ? { reasoning: { levels: model.reasoning.levels, parameter: model.reasoning.parameter || 'effort' } } : {}) }))
}
