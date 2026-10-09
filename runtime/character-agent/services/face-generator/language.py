"""Chinese test input conversion through the already configured dialogue provider."""
import time
from emotion import english,EmotionState
def describe(text,emotion,complete,cfg,state=None):
    if not isinstance(text,str) or not text.strip() or len(text)>1000:raise ValueError('表演文字需要1–1000字符')
    EmotionState.resolve(emotion,state)
    direct=english(text)
    if direct:return {'text':direct,'emotion':emotion,'state':state,'translated':False,'translation_ms':0}
    started=time.monotonic()
    messages=[{'role':'system','content':'把用户表演文字转换成内部英文情绪台词。只输出JSON: {"reply":"简短中文转换说明","mode":"keep","steps":[],"emotion":"用户指定标签","face_text":"3–20词英文情绪台词"}。标签只能neutral/happy/sad/angry/relaxed。face_text表达文字的情绪，不写身体动作。不要请求工具或改变用户的情绪标签。'},
              {'role':'user','content':__import__('json').dumps({'text':text,'emotion':emotion},ensure_ascii=False)}]
    plan=complete(cfg,messages);description=english(plan.face_text)
    if not description:raise ValueError('AI未返回有效英文表演描述，请重试或检查接口模型')
    return {'text':description,'emotion':emotion,'state':state,'translated':True,'translation_ms':round((time.monotonic()-started)*1000,2)}
