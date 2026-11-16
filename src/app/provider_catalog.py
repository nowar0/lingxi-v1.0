"""Bundled official-document rules, reviewed 2026-10-28; not account availability."""
from copy import deepcopy
from app import catalog

PRESETS = [
    {'id':'openai','name':'OpenAI','base_url':'https://api.openai.com/v1','protocol':'openai',
     'documentation':'https://developers.openai.com/api/docs/guides/reasoning',
     'models':['gpt-6-astra','gpt-6.1-sol','gpt-5.4','gpt-image-1','gpt-4o-mini-transcribe','gpt-4o-mini-tts']},
    {'id':'xai','name':'Grok · xAI','base_url':'https://api.x.ai/v1','protocol':'openai',
     'documentation':'https://docs.x.ai/developers/model-capabilities/text/reasoning',
     'models':['grok-4.7','grok-4.6','grok-imagine-image']},
    {'id':'gemini','name':'Gemini · Google','base_url':'https://generativelanguage.googleapis.com/v1beta/openai','protocol':'gemini',
     'documentation':'https://ai.google.dev/gemini-api/docs/openai',
     'models':['gemini-4-argon','gemini-3.8-flash','gemini-nano-banana-2.1','gemini-3.8-flash-tts']},
    {'id':'deepseek','name':'DeepSeek','base_url':'https://api.deepseek.com','protocol':'openai',
     'documentation':'https://api-docs.deepseek.com/api/list-models/',
     'models':['deepseek-flash','deepseek-v4-pro']},
    {'id':'kimi','name':'Kimi · Moonshot','base_url':'https://api.moonshot.cn/v1','protocol':'openai',
     'documentation':'https://platform.kimi.com/docs/models',
     'models':['kimi-k3.1-pro','kimi-k3.1-pro-highspeed','kimi-k3','kimi-k2.6']},
    {'id':'anthropic','name':'Claude · Anthropic','base_url':'https://api.anthropic.com/v1','protocol':'anthropic',
     'documentation':'https://platform.claude.com/docs/en/api/models/list',
     'models':['claude-opus-5-5','claude-sonnet-5-5','claude-haiku-5-5']},
    {'id':'glm','name':'GLM · 智谱','base_url':'https://open.bigmodel.cn/api/paas/v4','protocol':'openai',
     'documentation':'https://docs.bigmodel.cn/cn/guide/capabilities/thinking',
     'models':['glm-5.3','glm-5.3-flash','glm-5.2','glm-5v-turbo']},
    {'id':'custom','name':'自定义服务','base_url':'','protocol':'openai','documentation':'','models':[]},
]


def preset(key):
    return deepcopy(next((item for item in PRESETS if item['id']==key), PRESETS[-1]))


def documented(model):
    name=model.lower().split('/')[-1]
    inputs, outputs, caps, provider = [], [], [], ''
    if name.startswith('gpt-'):
        provider='openai'
        if 'transcribe' in name: inputs,outputs,caps=['audio'],['text'],['stt']
        elif 'tts' in name: inputs,outputs,caps=['text'],['audio'],['tts']
        elif name.startswith('gpt-image'): inputs,outputs,caps=['text','image'],['image'],['image']
        elif name.startswith(('gpt-4o','gpt-4.1','gpt-5','gpt-6')): inputs,outputs,caps=['text','image'],['text'],['text','vision']
    elif name.startswith('grok-'):
        provider='xai'
        if 'imagine-image' in name: inputs,outputs,caps=['text','image'],['image'],['image']
        elif name.startswith(('grok-4.7','grok-4.6','grok-4.5')): inputs,outputs,caps=['text','image'],['text'],['text','vision']
    elif name.startswith('gemini-'):
        provider='gemini'
        if 'tts' in name: inputs,outputs,caps=['text'],['audio'],['tts']
        elif 'image' in name or 'banana' in name: inputs,outputs,caps=['text','image'],['text','image'],['text','vision','image']
        elif name in ('gemini-4-argon','gemini-3.8-flash','gemini-3.6-flash','gemini-3.5-flash-lite','gemini-3.1-flash-lite'):
            inputs,outputs,caps=['text','image','audio','video','document'],['text'],['text','vision','stt']
    elif name.startswith('deepseek-'):
        provider='deepseek'
        if name=='deepseek-flash' or 'vision' in name: inputs,outputs,caps=['text','image'],['text'],['text','vision']
        elif name=='deepseek-v4-pro': inputs,outputs,caps=['text'],['text'],['text']
    elif name.startswith('kimi-'):
        provider='kimi'
        if name.startswith(('kimi-k3.1-pro','kimi-k3','kimi-k2.6')): inputs,outputs,caps=['text','image'],['text'],['text','vision']
    elif name.startswith('claude-'):
        provider='anthropic'
        if name.startswith(('claude-opus-','claude-sonnet-','claude-haiku-','claude-fable-','claude-mythos-')):
            inputs,outputs,caps=['text','image','document'],['text'],['text','vision']
    elif name.startswith('glm-'):
        provider='glm'
        if name=='glm-5v-turbo': inputs,outputs,caps=['text','image','video','document'],['text'],['text','vision']
        elif name.startswith(('glm-5.3','glm-5.2','glm-5.1','glm-4.7')): inputs,outputs,caps=['text'],['text'],['text']
    return {'input_modalities':inputs,'output_modalities':outputs,'capabilities':caps,
            'capability_source':'documentation' if caps else 'unknown','documentation':preset(provider)['documentation'] if provider else ''}


@catalog.router.get('/provider-presets')
def listing():
    from app.reasoning import describe
    return {'reviewed_at':'2026-10-28','items':[{**preset(item['id']), 'models':[
        {'id':model,'display_name':model,**documented(model),'reasoning':describe(model)} for model in item['models']]} for item in PRESETS]}
