from core import *


def say(m, lang, s):
    m.feed(tokenize(s, lang))
    m.end_utterance()
    return f"cur={m.cur} adlib={m.adlib} fin={m.finished}"


script = ("Good morning everyone.\nToday we talk about the new schedule and the budget.\n"
          "Please keep your questions until the end.")
m = Matcher()
m.profile = Profile(3, Lang.ENGLISH)
m.load(split_phrases(script, Lang.ENGLISH))
print("good morning    ->", say(m, Lang.ENGLISH, "good morning everyone"))
print("half of line 2  ->", say(m, Lang.ENGLISH, "today we talk about the new"))
print("adlib           ->", say(m, Lang.ENGLISH, "oh by the way my dog ate my homework yesterday"))
print("rejoin+finish   ->", say(m, Lang.ENGLISH, "schedule and the budget"))
print("line 3 skipping ->", say(m, Lang.ENGLISH, "please keep questions until the end"))

b = Matcher()
b.profile = Profile(3, Lang.BISAYA)
b.load(split_phrases("Maayong buntag kaninyong tanan.\nKaugmaon ang atong pagtigom karon.", Lang.BISAYA))
print("bisaya line 1   ->", say(b, Lang.BISAYA, "maayung buntag kaninyung tanan"))
print("bisaya line 2   ->", say(b, Lang.BISAYA, "kaugmaun ang atung pagtigum karun"))

# slow speaker: word by word through partial results
s = Matcher()
s.load(split_phrases("One two three four five six.\nSeven eight nine ten.", Lang.ENGLISH))
words = []
for w in "one two three four five six".split():
    words.append(w)
    s.feed(tokenize(" ".join(words), Lang.ENGLISH))
print("slow reader     -> cur=%d (expect 1)" % s.cur)
