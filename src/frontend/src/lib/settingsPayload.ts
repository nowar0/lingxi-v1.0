/** Server-owned metadata is read-only and must never return in strict update bodies. */
export function settingsPayload(value:{revision:number},fields:readonly string[]){
 const source=value as unknown as Record<string,unknown>
 return {...Object.fromEntries(fields.filter(key=>source[key]!==undefined).map(key=>[key,source[key]])),expected_revision:value.revision}
}
export const sessionOptionFields=['model','effort','temperature','tool_groups','voice_engine','auto_read'] as const
export const taskSettingFields=['daily_tokens','daily_media_calls','reply_tokens','reviewer','auto_archive_days'] as const
export const projectDocFields=['enabled','embedding_model','workspace_id','external_files'] as const
export const reminderFields=['session_id','kind','content','cron','enabled','daily_token_limit'] as const
export const memorySettingFields=['auto_memory_enabled','summarize_every_turns','keep_recent_turns','autoload_days','shared_auto_categories','semantic_enabled','semantic_max_chunks','context_tokens','reply_reserve_tokens','tool_reserve_tokens','memory_budget_tokens','rag_enabled','rag_top_k','rag_min_score'] as const
