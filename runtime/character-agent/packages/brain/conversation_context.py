"""Finite recent dialogue plus a rolling summary, updated in the normal planner response."""
from context import dialogue_history

RECENT_MESSAGES=8
SUMMARY_CHARS=800


class ConversationContext:
    def __init__(self,memory=None):
        self.summary=''
        self.source='none'
        self.folded_messages=0
        if memory:
            self.summary=str(memory.get('summary',''))[:SUMMARY_CHARS]
            self.source=str(memory.get('source','none'))
            self.folded_messages=int(memory.get('folded_messages',0))

    def snapshot(self):
        return dict(summary=self.summary,source=self.source,folded_messages=self.folded_messages,
                    summary_limit_chars=SUMMARY_CHARS,recent_limit_messages=RECENT_MESSAGES)

    def request(self,history):
        # After the forthcoming user/reply pair, fold the messages outside the recent window.
        count=max(0,len(history)+2-RECENT_MESSAGES)
        older=dialogue_history(history[:count],pairs=8,text=180)
        output=self.snapshot()
        output.update(update_required=bool(count),messages_to_fold=older)
        return output

    def fold(self,history,model_summary=None):
        count=max(0,len(history)-RECENT_MESSAGES)
        if not count:return history
        if isinstance(model_summary,str) and model_summary.strip():
            self.summary=model_summary.strip()[:SUMMARY_CHARS]
            self.source='model_summary'
        else:
            # Honest fallback: selected verbatim fragments, not neural semantic compression.
            fragments=[f"{m['role']}: {m['content']}" for m in dialogue_history(history[:count],pairs=8,text=140)]
            self.summary=('\n'.join([self.summary,*fragments]))[-SUMMARY_CHARS:]
            self.source='rule_excerpt'
        self.folded_messages+=count
        return history[-RECENT_MESSAGES:]
