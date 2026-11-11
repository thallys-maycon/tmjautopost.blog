"""Painel privado do TMJautopost (roda só no seu Codespaces/computador, NÃO vai pro site).
Rodar na raiz do projeto:  python admin/painel.py   ->  porta 5001
"""
import html, json, os, re, subprocess, unicodedata, uuid
from pathlib import Path
import requests
from flask import Flask, Response, jsonify, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DADOS = DOCS / "dados.json"
IMGS = DOCS / "imagens"
KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
SENHA = os.getenv("PAINEL_SENHA", "")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


@app.before_request
def auth():
    if SENHA:
        a = request.authorization
        if not a or a.password != SENHA:
            return Response("Senha necessária", 401, {"WWW-Authenticate": 'Basic realm="Painel"'})


def load():
    return json.loads(DADOS.read_text(encoding="utf-8")) if DADOS.exists() else {"cats": [], "prods": [], "guias": []}


def save(d):
    DADOS.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60] or "item"


def num(s):
    s = re.sub(r"[^\d.,]", "", str(s or ""))
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s) if s else 0.0


def fmt(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def esc(s):
    return html.escape(str(s or "").strip(), quote=True)


def lines(s):
    return [esc(x) for x in str(s or "").splitlines() if x.strip()]


def gemini(prompt):
    if not KEY:
        raise RuntimeError("Defina GEMINI_API_KEY no arquivo .env")
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        headers={"x-goog-api-key": KEY, "Content-Type": "application/json"}, timeout=90,
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6}})
    if r.status_code == 429:
        raise RuntimeError("Limite gratuito da IA atingido. Espere 1 minuto e tente de novo (se persistir, pode ser o limite do dia).")
    if r.status_code != 200:
        try:
            raise RuntimeError(r.json()["error"]["message"])
        except (KeyError, ValueError):
            raise RuntimeError(f"Erro na IA (HTTP {r.status_code})")
    parts = r.json()["candidates"][0]["content"]["parts"]
    txt = "".join(p.get("text", "") for p in parts).strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    return json.loads(txt)


REGRAS = ("Você é redator de um blog brasileiro de ofertas. Escreva em português do Brasil, tom amigável e honesto. "
          "Use SOMENTE os dados fornecidos: NÃO invente especificações, números, certificações, garantias ou marcas. "
          "Se faltar informação, seja genérico. Responda APENAS com JSON válido.")


@app.post("/api/gerar-produto")
def gerar_produto():
    j = request.get_json(force=True)
    if not j.get("nome"):
        return jsonify(erro="Preencha o nome do produto."), 400
    prompt = (REGRAS + '\nChaves do JSON: "titulo" (nome limpo, até 80 caracteres), "resumo" (1 frase, até 110 caracteres), '
              '"texto" (2 a 3 frases), "pros" (lista de 3 textos curtos), "contras" (lista de 1 a 2 pontos de atenção honestos).\n'
              f"Nome: {j.get('nome')}\nPreço: R$ {j.get('preco')} (de R$ {j.get('de')})\nNota: {j.get('nota')}/5\n"
              f"Informações copiadas da página do produto:\n{str(j.get('info') or '')[:3000]}")
    try:
        return jsonify(gemini(prompt))
    except Exception as e:
        return jsonify(erro=str(e)), 502


@app.post("/api/salvar-produto")
def salvar_produto():
    j = request.get_json(force=True)
    link, titulo, catnome = str(j.get("link", "")).strip(), esc(j.get("titulo")), esc(j.get("cat"))
    if not link.startswith(("http://", "https://")):
        return jsonify(erro="O link de afiliado precisa começar com https://"), 400
    if not titulo or not catnome:
        return jsonify(erro="Título e categoria são obrigatórios (gere o texto primeiro)."), 400
    d = load()
    cid = slug(str(j.get("cat", "")))
    if not any(c["id"] == cid for c in d["cats"]):
        d["cats"].append({"id": cid, "nome": catnome, "icon": esc(j.get("icon")) or "📦"})
    preco, de = num(j.get("preco")), num(j.get("de"))
    off = round((de - preco) / de * 100) if de > preco > 0 else 0
    base = slug(str(j.get("titulo", "")))
    pid, n = base, 2
    while any(p["id"] == pid for p in d["prods"]):
        pid, n = f"{base[:55]}-{n}", n + 1
    img = str(j.get("img", "")).strip()
    cat = next(c for c in d["cats"] if c["id"] == cid)
    d["prods"].insert(0, {
        "id": pid, "cat": cid, "icon": cat["icon"], "titulo": titulo,
        "img": esc(img) if img.startswith(("http", "imagens/")) else "",
        "nota": max(1, min(5, round(num(j.get("nota")) or 5))),
        "preco": fmt(preco), "de": fmt(de or preco), "off": off,
        "resumo": esc(j.get("resumo")), "texto": esc(j.get("texto")),
        "pros": lines(j.get("pros")), "contras": lines(j.get("contras")), "link": esc(link)})
    save(d)
    return jsonify(ok=True)


@app.post("/api/gerar-guia")
def gerar_guia():
    j = request.get_json(force=True)
    d = load()
    ps = [p for p in d["prods"] if p["id"] in j.get("ids", [])]
    if len(ps) < 2:
        return jsonify(erro="Escolha pelo menos 2 produtos."), 400
    dados = "\n".join(f'- id={p["id"]} | {p["titulo"]} | R$ {p["preco"]} | nota {p["nota"]}/5 | {p["resumo"]} | {p["texto"]}' for p in ps)
    prompt = (REGRAS + f'\nTema do guia: "{j.get("tema")}". Ordene os produtos do melhor para o pior para esse tema. '
              'JSON: {"titulo": "...", "intro": "2 frases", "itens": [{"p": "<id exato>", "label": "ex.: Melhor custo-benefício (até 4 palavras)", '
              '"veredito": "1 a 2 frases"}]}. Use cada produto uma única vez e os ids exatos.\n' + dados)
    try:
        g = gemini(prompt)
        ids = {p["id"] for p in ps}
        g["itens"] = [i for i in g.get("itens", []) if i.get("p") in ids]
        return jsonify(g)
    except Exception as e:
        return jsonify(erro=str(e)), 502


@app.post("/api/salvar-guia")
def salvar_guia():
    j = request.get_json(force=True)
    d = load()
    itens = [{"p": i["p"], "label": esc(i.get("label")), "veredito": esc(i.get("veredito"))}
             for i in j.get("itens", []) if any(p["id"] == i["p"] for p in d["prods"])]
    cat = next((c for c in d["cats"] if c["id"] == j.get("cat")), None)
    if not itens or not cat or not j.get("titulo"):
        return jsonify(erro="Gere o guia primeiro (título, categoria e itens são obrigatórios)."), 400
    gid, n = slug(j["titulo"]), 2
    while any(g["id"] == gid for g in d["guias"]):
        gid, n = f"{slug(j['titulo'])[:55]}-{n}", n + 1
    d["guias"].insert(0, {"id": gid, "cat": cat["id"], "icon": cat["icon"], "titulo": esc(j["titulo"]),
                          "intro": esc(j.get("intro")), "itens": itens})
    save(d)
    return jsonify(ok=True)


@app.post("/api/upload")
def upload():
    f = request.files.get("f")
    ext = (f.filename.rsplit(".", 1)[-1].lower() if f and "." in f.filename else "")
    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
        return jsonify(erro="Envie uma imagem jpg, png, webp ou gif."), 400
    IMGS.mkdir(exist_ok=True)
    nome = f"{uuid.uuid4().hex[:10]}.{ext}"
    f.save(IMGS / nome)
    return jsonify(path=f"imagens/{nome}")


@app.post("/api/excluir")
def excluir():
    j = request.get_json(force=True)
    d = load()
    if j.get("tipo") == "prod":
        d["prods"] = [p for p in d["prods"] if p["id"] != j["id"]]
        for g in d["guias"]:
            g["itens"] = [i for i in g["itens"] if i["p"] != j["id"]]
        d["guias"] = [g for g in d["guias"] if g["itens"]]
    else:
        d["guias"] = [g for g in d["guias"] if g["id"] != j["id"]]
    save(d)
    return jsonify(ok=True)


@app.get("/api/dados")
def dados():
    return jsonify(load())


@app.post("/api/publicar")
def publicar():
    def git(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True)
    git("add", "docs")
    c = git("commit", "-m", "Atualiza site")
    if "nothing to commit" in (c.stdout + c.stderr):
        return jsonify(msg="Nada novo para publicar.")
    if c.returncode != 0:
        return jsonify(erro="Erro no commit: " + (c.stderr or c.stdout)[:300]), 500
    p = git("push")
    if p.returncode != 0:
        return jsonify(erro="Erro no push: " + p.stderr[:300]), 500
    return jsonify(msg="Publicado! O site atualiza em 1 a 2 minutos.")


@app.get("/")
def home():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Painel TMJautopost</title><style>
body{margin:0;background:#050805;color:#e6ffe6;font-family:system-ui,sans-serif;padding:20px;max-width:860px;margin:auto}
h1{color:#39ff14}h2{color:#39ff14;border-bottom:1px solid #143314;padding-bottom:6px;margin-top:34px}h3{color:#7fa07f}
input,textarea,select{width:100%;background:#0b110b;color:#e6ffe6;border:1px solid #143314;padding:10px;border-radius:6px;margin:4px 0 10px;font:inherit}
textarea{min-height:70px}label{font-size:.85rem;color:#7fa07f}.g{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
button{background:transparent;color:#39ff14;border:2px solid #39ff14;padding:10px 16px;border-radius:6px;cursor:pointer;font-weight:700;margin:4px 6px 4px 0}
button:hover{background:#39ff14;color:#000}.box{background:#0b110b;border:1px solid #143314;padding:10px;margin:8px 0;border-radius:6px}
.row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #143314}
#msg{position:sticky;top:0;background:#050805;padding:10px 0;font-weight:700;z-index:5}</style></head><body>
<h1>🎮 Painel TMJautopost</h1><div id="msg">Pronto.</div>
<button onclick="pub()">🚀 PUBLICAR NO SITE</button>

<h2>1. Novo produto</h2>
<label>Link de afiliado *</label><input id="link" placeholder="https://s.shopee.com.br/...">
<label>Nome do produto (copie da Shopee) *</label><input id="nome">
<div class="g"><div><label>Categoria * (escolha uma existente ou digite uma nova)</label><input id="cat" list="cats"></div>
<div><label>Ícone (emoji, só para categoria nova)</label><input id="icon" placeholder="🎧"></div>
<div><label>Preço atual (R$)</label><input id="preco"></div><div><label>Preço original (R$)</label><input id="de"></div>
<div><label>Nota (1 a 5)</label><input id="nota" value="5"></div>
<div><label>Imagem: cole uma URL ou envie um arquivo</label><input id="img" placeholder="https://..."></div></div>
<input type="file" accept="image/*" onchange="up(this)">
<label>Informações copiadas da página da Shopee (descrição, especificações). Quanto mais, melhor o texto.</label><textarea id="info" style="min-height:110px"></textarea>
<button onclick="gerarP()">✨ Gerar texto com IA</button>
<div id="fp"><label>Título</label><input id="titulo"><label>Resumo (1 frase)</label><input id="resumo">
<label>Texto</label><textarea id="texto"></textarea>
<div class="g"><div><label>Pontos fortes (1 por linha)</label><textarea id="pros"></textarea></div><div><label>Pontos de atenção (1 por linha)</label><textarea id="contras"></textarea></div></div></div>
<button onclick="salvarP()">💾 Salvar produto</button>

<h2>2. Novo guia / comparativo</h2>
<label>Tema (ex.: Os melhores fones Bluetooth de 2026)</label><input id="tema">
<label>Categoria</label><select id="gcat" onchange="gprods()"></select>
<label>Produtos do guia (mínimo 2)</label><div id="gp" class="box"></div>
<button onclick="gerarG()">✨ Gerar guia com IA</button>
<label>Título do guia</label><input id="gtit"><label>Introdução</label><textarea id="gintro"></textarea><div id="gi"></div>
<button onclick="salvarG()">💾 Salvar guia</button>

<h2>3. Conteúdo atual</h2><div id="cur"></div><datalist id="cats"></datalist>
<script>
const $=s=>document.querySelector(s),q=s=>String(s??"").replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;");
let D={cats:[],prods:[],guias:[]},GI=[];
function msg(t,ok){const m=$("#msg");m.textContent=t;m.style.color=ok===false?"#ff2e88":"#39ff14"}
async function api(u,b){const r=await fetch(u,{method:b?"POST":"GET",headers:{"Content-Type":"application/json"},body:b?JSON.stringify(b):undefined});
 const d=await r.json();if(!r.ok)throw new Error(d.erro||"Erro");return d}
async function load(){D=await api("/api/dados");
 $("#cats").innerHTML=D.cats.map(c=>`<option value="${q(c.nome)}">`).join("");
 $("#gcat").innerHTML=D.cats.map(c=>`<option value="${q(c.id)}">${c.icon} ${c.nome}</option>`).join("");gprods();
 $("#cur").innerHTML="<h3>Produtos ("+D.prods.length+")</h3>"+D.prods.map(p=>`<div class="row"><span>${p.icon} ${p.titulo}</span><button onclick="del('prod','${p.id}')">🗑️</button></div>`).join("")+
 "<h3>Guias ("+D.guias.length+")</h3>"+D.guias.map(g=>`<div class="row"><span>${g.icon} ${g.titulo}</span><button onclick="del('guia','${g.id}')">🗑️</button></div>`).join("")}
async function gerarP(){msg("Gerando texto...");try{const d=await api("/api/gerar-produto",{nome:$("#nome").value,info:$("#info").value,preco:$("#preco").value,de:$("#de").value,nota:$("#nota").value});
 $("#titulo").value=d.titulo||"";$("#resumo").value=d.resumo||"";$("#texto").value=d.texto||"";$("#pros").value=(d.pros||[]).join("\n");$("#contras").value=(d.contras||[]).join("\n");msg("Texto gerado. REVISE e depois salve.")}catch(e){msg(e.message,false)}}
async function salvarP(){try{await api("/api/salvar-produto",{link:$("#link").value,cat:$("#cat").value,icon:$("#icon").value,preco:$("#preco").value,de:$("#de").value,nota:$("#nota").value,img:$("#img").value,titulo:$("#titulo").value,resumo:$("#resumo").value,texto:$("#texto").value,pros:$("#pros").value,contras:$("#contras").value});
 document.querySelectorAll("input:not([type=file]):not(#nota),textarea").forEach(e=>{if(!["tema","gtit","gintro"].includes(e.id))e.value=""});
 msg("Produto salvo! Clique em PUBLICAR NO SITE quando quiser colocar no ar.");await load()}catch(e){msg(e.message,false)}}
async function up(inp){const f=inp.files[0];if(!f)return;const fd=new FormData();fd.append("f",f);const r=await fetch("/api/upload",{method:"POST",body:fd});const d=await r.json();
 if(r.ok){$("#img").value=d.path;msg("Imagem enviada.")}else msg(d.erro,false)}
function gprods(){const c=$("#gcat").value;$("#gp").innerHTML=D.prods.filter(p=>p.cat===c).map(p=>`<label><input type="checkbox" value="${q(p.id)}" style="width:auto;margin-right:8px">${p.titulo}</label><br>`).join("")||"Nenhum produto nessa categoria ainda."}
async function gerarG(){const ids=[...document.querySelectorAll("#gp input:checked")].map(x=>x.value);if(ids.length<2)return msg("Marque pelo menos 2 produtos.",false);
 msg("Gerando guia...");try{const d=await api("/api/gerar-guia",{tema:$("#tema").value,ids});$("#gtit").value=d.titulo||"";$("#gintro").value=d.intro||"";GI=d.itens||[];rgi();msg("Guia gerado. REVISE e depois salve.")}catch(e){msg(e.message,false)}}
function rgi(){$("#gi").innerHTML=GI.map((x,i)=>{const p=D.prods.find(p=>p.id===x.p);return `<div class="box"><b>#${i+1} ${p?p.titulo:x.p}</b><input value="${q(x.label)}" oninput="GI[${i}].label=this.value"><textarea oninput="GI[${i}].veredito=this.value">${q(x.veredito)}</textarea></div>`}).join("")}
async function salvarG(){try{await api("/api/salvar-guia",{titulo:$("#gtit").value,intro:$("#gintro").value,cat:$("#gcat").value,itens:GI});GI=[];rgi();$("#gtit").value=$("#gintro").value="";msg("Guia salvo! Clique em PUBLICAR NO SITE.");await load()}catch(e){msg(e.message,false)}}
async function del(t,id){if(!confirm("Excluir?"))return;await api("/api/excluir",{tipo:t,id});await load()}
async function pub(){msg("Publicando...");try{const d=await api("/api/publicar",{});msg(d.msg)}catch(e){msg(e.message,false)}}
load();
</script></body></html>"""


if __name__ == "__main__":
    app.run(port=5001, debug=False)
