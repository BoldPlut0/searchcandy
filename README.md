# searchcandy

Grounded answers from your documents — or an honest "not in your
data." Never an invention. Proven.

```python
# export SEARCHCANDY_API_KEY=sc_live_...
from searchcandy import SearchCandy

sc = SearchCandy()
sc.ingest_folder("./contracts")

r = sc.query("What is the termination notice period?")
print(r.quotes[0].text, "—", r.quotes[0].source_id)

t = sc.query("What does clause 99.9 say?")   # not in your data
print(t.refused)   # True — zero tokens, no invention
```

Docs: https://docs.searchcandy-labs.com
