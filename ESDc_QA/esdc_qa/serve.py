"""Zero-dependency local web UI (fallback if Streamlit isn't installed).

Run:  python3 -m esdc_qa.serve          (opens http://localhost:8000)

The richer UI is the Streamlit app (`streamlit run app.py`). This stdlib server
offers the same modes with no pip installs. Frontend reads the CSV as text and
POSTs it, so there's no multipart parsing.
"""
from __future__ import annotations

import json
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .expansion_loader import load_expansion
from .runners import (describe_esdc, pick_esdc, run_audit, run_explain,
                      run_fn_report, run_fn_sql)
from .semantics import load_semantics

HERE = os.path.dirname(__file__)
EXPANSION_PATH = os.path.join(HERE, "expansion.json")
_ESDCS = load_expansion(EXPANSION_PATH)
_SEM = load_semantics(HERE)
with open(EXPANSION_PATH, encoding="utf-8-sig") as _f:
    _EXPANSION = json.load(_f)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if self.path == "/api/esdcs":
            return self._send(200, json.dumps({"esdcs": sorted(_ESDCS)}))
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/api/run":
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            mode = req.get("mode", "explain")
            if mode == "audit":
                cols, rows = run_audit(_ESDCS, _SEM)
                return self._send(200, json.dumps({"columns": cols, "rows": rows}))
            e = pick_esdc(_ESDCS, req["esdc"])
            if mode == "describe":
                d = describe_esdc(e, _SEM, _EXPANSION)
                txt = (f"{d['name']}   (id {d['id']}, stemming={d['stemming']})\n\n"
                       f"OUTPUT TOPIC(S): {', '.join(d['output_topics']) or '—'}\n"
                       f"OUTPUT REFERENCE TERM(S): {', '.join(d['output_keys']) or '—'}\n\n"
                       f"WHAT MAKES AN ALERT MATCH:\n{d['plain_md']}")
                res = {"text": txt}
            elif mode == "explain":
                cols, rows = run_explain(e, _SEM, req["csv"])
                res = {"columns": cols, "rows": rows}
            elif mode == "fn-report":
                cols, rows = run_fn_report(e, _SEM, _EXPANSION, req["csv"])
                res = {"columns": cols, "rows": rows}
            elif mode == "fn-sql":
                res = {"sql": run_fn_sql(e, _SEM, int(req.get("days", 7)))}
            else:
                res = {"error": f"unknown mode {mode}"}
            return self._send(200, json.dumps(res))
        except Exception as ex:
            return self._send(200, json.dumps({"error": f"{type(ex).__name__}: {ex}"}))


def main(port=8000, open_browser=True):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"ESDc QA UI running at {url}  (Ctrl-C to stop)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>ESDc QA</title>
<style>
  :root{--navy:#0F1722;--slate:#1E2A38;--teal:#57C7BC;--blue:#2E9BE0;--line:#2a3a4d;}
  *{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
  body{margin:0;background:var(--navy);color:#E6EAEF}
  header{background:#0b121c;border-bottom:1px solid var(--line);padding:14px 22px}
  .tag{color:var(--teal);font-weight:700;letter-spacing:.14em;font-size:.7rem}
  h1{font-size:1.3rem;margin:.2rem 0}
  .wrap{padding:18px 22px;max-width:1200px}
  label{display:block;color:var(--teal);font-size:.7rem;letter-spacing:.12em;margin:14px 0 4px}
  select,input{background:var(--slate);color:#E6EAEF;border:1px solid var(--line);border-radius:6px;padding:8px;min-width:340px}
  .drop{margin-top:8px;border:2px dashed var(--line);border-radius:10px;background:var(--slate);
        padding:26px;text-align:center;color:#9fb0c0;cursor:pointer}
  .drop.over{border-color:var(--teal);color:#E6EAEF}
  button{background:var(--blue);color:#fff;border:0;border-radius:6px;padding:9px 16px;
         font-weight:700;letter-spacing:.04em;cursor:pointer;margin-top:14px}
  button.ghost{background:var(--slate);border:1px solid var(--line)}
  table{border-collapse:collapse;width:100%;margin-top:14px;font-size:.82rem}
  th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top;max-width:520px}
  th{background:var(--slate);position:sticky;top:0}
  .scroll{max-height:60vh;overflow:auto;border:1px solid var(--line);border-radius:8px;margin-top:12px}
  pre{background:var(--slate);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;max-height:60vh}
  .err{color:#ff8080}
</style></head><body>
<header><div class="tag">DATAMINR · ESDC QA</div><h1>Match explainer & false-negative finder</h1>
<div style="color:#9fb0c0;font-size:.82rem;margin-top:4px">Get test-data CSVs (by ESDC Name + Alert ID) from
<a href="https://dataminr.looker.com/dashboards/5718" style="color:#57C7BC">Looker dashboard 5718</a>.</div></header>
<div class="wrap">
  <label>MODE</label>
  <select id="mode" onchange="syncMode()">
    <option value="describe">Understand an ESDC</option>
    <option value="explain">Alert Match Query</option>
    <option value="fn-report">False-negative report</option>
    <option value="fn-sql">Generate FN SQL</option>
    <option value="audit">Library audit</option>
  </select>
  <span id="esdcWrap"><label>ESDC</label><select id="esdc"></select></span>
  <span id="daysWrap" style="display:none"><label>LOOKBACK (DAYS)</label><input id="days" type="number" value="7"></span>
  <div id="dropWrap"><label>RESULTS CSV</label>
    <div class="drop" id="drop">Drag & drop a CSV here, or click to choose</div>
    <input id="file" type="file" accept=".csv" style="display:none">
  </div>
  <div><button onclick="run()">Run</button>
       <button class="ghost" onclick="dl()" id="dlBtn" style="display:none">⬇ Download</button></div>
  <div id="status"></div>
  <div id="out"></div>
</div>
<script>
let CSV=null, RESULT=null;
fetch('/api/esdcs').then(r=>r.json()).then(d=>{
  document.getElementById('esdc').innerHTML=d.esdcs.map(n=>`<option>${n}</option>`).join('');
});
function syncMode(){
  const m=document.getElementById('mode').value;
  document.getElementById('esdcWrap').style.display = m==='audit'?'none':'inline';
  document.getElementById('daysWrap').style.display = m==='fn-sql'?'inline':'none';
  document.getElementById('dropWrap').style.display = (m==='explain'||m==='fn-report')?'block':'none';
  document.getElementById('esdcWrap').style.display = m==='audit'?'none':'inline';
}
const drop=document.getElementById('drop'), file=document.getElementById('file');
drop.onclick=()=>file.click();
file.onchange=e=>readFile(e.target.files[0]);
drop.ondragover=e=>{e.preventDefault();drop.classList.add('over')};
drop.ondragleave=()=>drop.classList.remove('over');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');readFile(e.dataTransfer.files[0])};
function readFile(f){if(!f)return;const r=new FileReader();r.onload=()=>{CSV=r.result;drop.textContent='✓ '+f.name};r.readAsText(f)}
function run(){
  const mode=document.getElementById('mode').value;
  const body={mode, esdc:document.getElementById('esdc').value, csv:CSV, days:document.getElementById('days').value};
  document.getElementById('status').textContent='Running…';
  fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
   .then(r=>r.json()).then(render);
}
function render(d){
  const out=document.getElementById('out'), st=document.getElementById('status');
  document.getElementById('dlBtn').style.display='none'; st.textContent='';
  if(d.error){out.innerHTML='<p class="err">'+d.error+'</p>';return}
  if(d.text){RESULT=null;out.innerHTML='<pre>'+d.text.replace(/</g,'&lt;')+'</pre>';return}
  if(d.sql){RESULT={sql:d.sql};out.innerHTML='<pre>'+d.sql.replace(/</g,'&lt;')+'</pre>';
    document.getElementById('dlBtn').style.display='inline';return}
  RESULT={columns:d.columns,rows:d.rows};
  let h='<div class="scroll"><table><tr>'+d.columns.map(c=>'<th>'+c+'</th>').join('')+'</tr>';
  h+=d.rows.map(r=>'<tr>'+r.map(c=>'<td>'+String(c==null?'':c).replace(/</g,'&lt;')+'</td>').join('')+'</tr>').join('');
  h+='</table></div>'; out.innerHTML=h; st.textContent=d.rows.length+' rows';
  document.getElementById('dlBtn').style.display='inline';
}
function dl(){
  if(!RESULT)return;
  let blob,name;
  if(RESULT.sql){blob=new Blob([RESULT.sql],{type:'text/plain'});name='fn_scan.sql';}
  else{const esc=s=>'"'+String(s==null?'':s).replace(/"/g,'""')+'"';
    const csv=[RESULT.columns.join(',')].concat(RESULT.rows.map(r=>r.map(esc).join(','))).join('\\n');
    blob=new Blob([csv],{type:'text/csv'});name='esdc_qa_results.csv';}
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;a.click();
}
syncMode();
</script></body></html>"""


if __name__ == "__main__":
    import sys
    main(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
