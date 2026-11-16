from app.tools.capabilities import spec

def unavailable(ctx,**kwargs):
    raise ValueError('提问卡片需要实时对话界面')

TOOL={'spec':spec('request_user_input','关键信息缺失或需要用户选择时，提出1～3个简短问题并等待回答。支持选项和自由填写；不要无必要地打断。回答不代替写入或执行授权。',{
    'questions':{'type':'array','minItems':1,'maxItems':3,'items':{'type':'object','additionalProperties':False,'properties':{
        'id':{'type':'string','description':'唯一短编号，只含字母、数字、下划线或连字符'},
        'question':{'type':'string','description':'简短完整的问题'},
        'options':{'type':'array','maxItems':6,'items':{'type':'object','additionalProperties':False,'properties':{'label':{'type':'string'},'description':{'type':'string'}},'required':['label']}},
        'multiple':{'type':'boolean','description':'是否允许多选；默认单选'}},'required':['id','question']}}},['questions']),
    'write':False,'run':unavailable,'timeout':1800}
