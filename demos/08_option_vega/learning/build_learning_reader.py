"""把同目录讲义与答案编译成可离线阅读的单文件HTML。"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from markdown_it import MarkdownIt

HERE = Path(__file__).resolve().parent
HANDBOOK = HERE / "策略流程学习手册.md"
ANSWERS = HERE / "习题答案与推导.md"
RECORD = HERE / "学习记录.md"
OUTPUT = HERE / "策略流程学习手册.html"
QUESTION = re.compile(r"^### ([BQR]\d{2}) ([^\n]+)\n(.*?)(?=^#{1,3} |\Z)", re.M | re.S)
ANSWER = re.compile(r"^## ([BQR]\d{2}) ([^\n]+)\n(.*?)(?=^## |\Z)", re.M | re.S)


def build() -> dict:
    source = HANDBOOK.read_text()
    answer_map = {m[1]: m[3].strip() for m in ANSWER.finditer(ANSWERS.read_text())}
    questions = list(QUESTION.finditer(source))
    question_ids = [m[1] for m in questions]
    assert len(question_ids) == len(set(question_ids))
    assert set(question_ids) == set(answer_map), (set(question_ids) ^ set(answer_map))
    toc = []
    md = MarkdownIt("commonmark", {"html": False}).enable("table")

    def heading_open(tokens, index, options, env):
        level = int(tokens[index].tag[1])
        env["serial"] = env.get("serial", 0) + 1
        title = tokens[index + 1].content
        ident = f'{env.get("prefix", "main")}-{env["serial"]}'
        tokens[index].attrSet("id", ident)
        if level == 2 and env.get("collect_toc"):
            toc.append((ident, title))
        return md.renderer.renderToken(tokens, index, options, env)

    md.renderer.rules["heading_open"] = heading_open
    env = {"prefix": "main", "collect_toc": True}
    body = []
    cursor = 0
    source = re.sub(r"^# [^\n]+\n", "", source, count=1)
    questions = list(QUESTION.finditer(source))
    for m in questions:
        body.append(md.render(source[cursor:m.start()], env))
        qid, title, stem = m[1], m[2], m[3]
        key = qid.lower()
        answer_html = md.render(answer_map[qid], {"prefix": f"a-{key}"})
        body.append(
            f'<section class="exercise" data-qid="{qid}" aria-labelledby="{key}">'
            f'<h3 id="{key}">{qid} {html.escape(title)}</h3>'
            f'{md.render(stem, env)}'
            f'<label class="draft-label" for="draft-{key}">我的结论、计算过程和理由</label>'
            f'<textarea id="draft-{key}" data-draft="{qid}" rows="4" '
            'placeholder="先写自己的答案；若用过提示，也在这里注明。"></textarea>'
            f'<label class="done-label"><input type="checkbox" data-done="{qid}"> '
            '记录为已练习（不自动评定掌握等级）</label>'
            f'<details class="answer" id="answer-{key}"><summary>核对 {qid} 的推导与易错点</summary>'
            f'<div class="answer-content">{answer_html}</div></details></section>'
        )
        cursor = m.end()
    body.append(md.render(source[cursor:], env))
    record = re.sub(r"^# [^\n]+\n", "", RECORD.read_text(), count=1)
    record = re.sub(r"^## ", "### ", record, flags=re.M)
    body.append('<section class="record"><h2 id="learning-record">学习记录快照</h2>')
    body.append(md.render(record, {"prefix": "record"}))
    body.append('</section><section><h2 id="answer-guide">怎样核对答案</h2>'
                '<p>每道题后均有默认收起的推导。先写自己的答案，再展开核对。'
                '完整答案原稿与学习记录也保存在同目录，方便用本地编辑器查看。</p></section>')
    article = "\n".join(body)
    for name, target in [("习题答案与推导.md", "#answer-guide"),
                         ("学习记录.md", "#learning-record"),
                         ("策略流程学习手册.md", "#top")]:
        article = article.replace(f'href="{name}"', f'href="{target}"')
        from urllib.parse import quote
        article = article.replace(f'href="{quote(name)}"', f'href="{target}"')
    nav = "".join(f'<a href="#{ident}">{html.escape(title)}</a>' for ident, title in toc)
    options = "".join(f'<option value="{m[1].lower()}">{m[1]} {html.escape(m[2])}</option>'
                      for m in questions)
    document = TEMPLATE.replace("__NAV__", nav).replace("__OPTIONS__", options).replace("__ARTICLE__", article)
    OUTPUT.write_text(document)
    return {"file": str(OUTPUT), "questions": len(questions), "answers": len(answer_map),
            "chapters": len(toc), "bytes": OUTPUT.stat().st_size}


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>MO认购策略流程学习手册</title>
<style>
:root{color-scheme:light dark;--bg:#f7f8fa;--paper:#fff;--ink:#202831;--muted:#54616e;--line:#d8dfe7;--accent:#175fa0;--soft:#edf4fa;--code:#f0f2f5}
@media(prefers-color-scheme:dark){:root{--bg:#171b20;--paper:#20262d;--ink:#e6ebf0;--muted:#b2bfcc;--line:#46525e;--accent:#99caff;--soft:#263748;--code:#181e25}}
*{box-sizing:border-box}html{scroll-padding-top:calc(var(--toolbar-height,5rem) + 1rem)}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
a{color:var(--accent);text-underline-offset:.2em}button,input,select,textarea{font:inherit}button,select{padding:.38rem .65rem;background:var(--paper);color:var(--ink);border:1px solid var(--line);border-radius:6px;max-width:100%}button{cursor:pointer}button:focus-visible,a:focus-visible,textarea:focus-visible,select:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
.toolbar{position:sticky;top:0;z-index:2;background:var(--paper);border-bottom:1px solid var(--line);padding:.65rem 1.25rem;display:flex;gap:.65rem;align-items:center;flex-wrap:wrap}.toolbar strong{margin-right:auto}.toolbar a{white-space:nowrap}.layout{max-width:1360px;margin:auto;display:grid;grid-template-columns:265px minmax(0,1fr);gap:1.6rem;padding:1.5rem}
aside{position:sticky;top:6rem;align-self:start;max-height:80vh;overflow:auto;font-size:.86rem;padding-right:.7rem}aside a{display:block;padding:.22rem 0;text-decoration:none}aside h2{font-size:1rem}main{min-width:0;background:var(--paper);padding:2rem 2.6rem;box-shadow:0 1px 8px #0000000b;border-radius:10px}h1{font-size:1.9rem;line-height:1.4;margin:0 0 .7rem}h2{font-size:1.35rem;line-height:1.5;margin-top:2.5rem;padding-top:1.25rem;border-top:1px solid var(--line)}h3{font-size:1.08rem;line-height:1.5;margin-top:1.8rem}p{margin:.7rem 0}ul,ol{padding-left:1.5rem}li{margin:.35rem 0}pre{background:var(--code);padding:1rem;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65;font-size:.87rem;border-radius:6px}code{font-family:ui-monospace,SFMono-Regular,Consolas,"PingFang SC",monospace;overflow-wrap:anywhere}p code,li code{background:var(--code);padding:.08rem .25rem;border-radius:3px}table{border-collapse:collapse;width:100%;margin:1rem 0;font-size:.93rem;table-layout:auto}th,td{padding:.6rem .7rem;border:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}th{background:var(--soft);text-align:left}blockquote{border-left:3px solid var(--accent);margin:1rem 0;padding:.1rem 1rem;color:var(--muted)}.intro{color:var(--muted)}
.exercise{margin:1.8rem 0;padding:1.1rem 1.2rem;background:var(--soft);border-left:4px solid var(--accent);border-radius:4px}.exercise h3{margin-top:0}.draft-label{display:block;font-weight:500;margin-top:1rem}textarea{display:block;width:100%;resize:vertical;border:1px solid var(--line);border-radius:6px;padding:.7rem;background:var(--paper);color:var(--ink);margin:.4rem 0;line-height:1.65}.done-label{display:flex;align-items:flex-start;gap:.45rem;font-size:.88rem;color:var(--muted)}input[type=checkbox]{margin-top:.45rem;accent-color:var(--accent)}details.answer{background:var(--paper);margin-top:1rem;padding:.65rem .85rem;border-radius:6px}summary{cursor:pointer;color:var(--accent);font-weight:500}.answer-content{padding-top:.65rem}#save-status,#practice-count{font-size:.85rem;color:var(--muted)}.small{font-size:.86rem;color:var(--muted)}.jump{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin:.8rem 0}select{min-width:0}.record{margin-top:3rem}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
@media(max-width:900px){.layout{display:block;padding:.8rem}aside{position:static;max-height:none;overflow:visible;margin-bottom:1rem}aside details nav{columns:2;column-gap:1rem}main{padding:1.4rem}h1{font-size:1.55rem}.toolbar{padding:.5rem .8rem}.toolbar strong{width:100%}table{font-size:.85rem}th,td{padding:.4rem}}
@media(max-width:460px){body{font-size:15px}.layout{padding:.4rem}main{padding:1rem .7rem}.exercise{padding:.85rem .65rem}aside details nav{columns:1}table{font-size:.78rem}th,td{padding:.3rem}.toolbar select{width:100%}h1{font-size:1.4rem}.jump>*{max-width:100%}}
@media print{body{background:#fff;color:#000;font-size:11pt}.toolbar,aside,.jump,textarea,.draft-label,.done-label,details.answer,.small.local-note{display:none!important}.layout{display:block;padding:0;max-width:none}main{padding:0;box-shadow:none}.exercise{background:transparent;border:1px solid #bbb;break-inside:avoid}h2,h3{break-after:avoid}table,pre{break-inside:avoid}a{color:inherit}th{background:#eee}pre{background:#f7f7f7}h1{font-size:20pt}}
</style>
</head>
<body id="top">
<header class="toolbar"><strong>MO策略学习手册</strong><a id="resume" href="#q04">继续 Q04</a><span id="practice-count" aria-live="polite"></span><button type="button" id="export">导出我的作答</button><button type="button" id="print">打印讲义</button></header>
<div class="layout"><aside><details id="outline" open><summary>流程目录</summary><nav aria-label="流程目录">__NAV__<a href="#learning-record">学习记录快照</a></nav></details></aside>
<main><h1>MO认购策略流程学习手册</h1><p class="intro">按流程查概念、做计算、核对推导。全部正文与答案可离线阅读。</p>
<p class="small local-note">草稿和练习标记保存在此浏览器，不自动修改学习等级或Markdown原稿。请用“导出我的作答”备份；若浏览器限制本地存储，可复制到同目录学习记录。</p>
<div class="jump"><label for="question-select">直接到题目</label><select id="question-select">__OPTIONS__</select><button type="button" id="jump">定位</button><span id="save-status" aria-live="polite"></span></div>
<article>__ARTICLE__</article></main></div>
<script>
(()=>{
const storageKey='mo-learning-v1-20261119';
let state={drafts:{},done:{Q01:true,Q02:true},last:'q04'};
try{const saved=JSON.parse(localStorage.getItem(storageKey)||'null');if(saved&&typeof saved==='object'){state={...state,...saved,drafts:saved.drafts||{},done:saved.done||{}}}}catch(e){}
const status=document.getElementById('save-status');
const resume=document.getElementById('resume');
const select=document.getElementById('question-select');
const wide=window.matchMedia('(min-width:901px)');
const outline=document.getElementById('outline');
outline.open=wide.matches;
wide.addEventListener('change',event=>{outline.open=event.matches});
new ResizeObserver(entries=>{document.documentElement.style.setProperty('--toolbar-height',entries[0].target.getBoundingClientRect().height+'px')}).observe(document.querySelector('.toolbar'));
function update(){const count=[...document.querySelectorAll('[data-done]')].filter(x=>x.checked).length;document.getElementById('practice-count').textContent=`已标记练习 ${count} / ${document.querySelectorAll('[data-done]').length}`;const valid=document.getElementById(state.last);if(valid){resume.href='#'+state.last;resume.textContent='继续 '+state.last.toUpperCase();select.value=state.last}}
function save(){try{localStorage.setItem(storageKey,JSON.stringify(state));status.textContent='草稿已保存到此浏览器'}catch(e){status.textContent='本地存储不可用，请导出作答备份'}update()}
document.querySelectorAll('[data-draft]').forEach(el=>{el.value=state.drafts[el.dataset.draft]||'';el.addEventListener('input',()=>{state.drafts[el.dataset.draft]=el.value;state.last=el.dataset.draft.toLowerCase();save()});el.addEventListener('focus',()=>{state.last=el.dataset.draft.toLowerCase();save()})});
document.querySelectorAll('[data-done]').forEach(el=>{el.checked=Boolean(state.done[el.dataset.done]);el.addEventListener('change',()=>{state.done[el.dataset.done]=el.checked;state.last=el.dataset.done.toLowerCase();save()})});
document.getElementById('jump').addEventListener('click',()=>{const target=document.getElementById(select.value);if(target){state.last=select.value;save();target.scrollIntoView({behavior:'auto',block:'start'})}});
document.getElementById('export').addEventListener('click',()=>{const out={...state,note:'练习记录，不是掌握等级评估',document:'MO认购策略流程学习手册'};const url=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{type:'application/json;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download='MO策略我的作答.json';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)});
document.getElementById('print').addEventListener('click',()=>window.print());
update();
})();
</script>
</body></html>'''


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
