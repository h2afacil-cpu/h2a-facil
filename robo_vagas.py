"""Robô diário: baixa as vagas H-2A oficiais e grava no Supabase."""
import io, os, re, json, zipfile, datetime
import numpy as np
import pandas as pd
import requests
import geonamescache
import pgeocode

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
CHAVE = os.environ["SUPABASE_SECRET_KEY"]
POP_MIN = 50000
hoje = datetime.date.today()


def baixar(tipo):
    for d in range(5):
        dia = (hoje - datetime.timedelta(days=d)).isoformat()
        url = f"https://api.seasonaljobs.dol.gov/datahub-search/sjCaseData/zip/{tipo}/{dia}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=180)
        if r.status_code == 200 and len(r.content) > 1000:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            dados = json.loads(z.read(z.namelist()[0]))
            if isinstance(dados, dict):
                dados = next((v for v in dados.values() if isinstance(v, list)), [])
            print(tipo, dia, len(dados), "registros")
            return dados
    raise SystemExit("Não consegui baixar " + tipo)


def achatar(d, pre=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(achatar(v, pre + k + "."))
        elif isinstance(v, list):
            out[pre + k] = json.dumps(v, ensure_ascii=False)
        else:
            out[pre + k] = v
    return out


def pega(flat, sufixo):
    for k, v in flat.items():
        if k.endswith(sufixo) and v not in (None, "", "None"):
            return v
    return None


def achar(flat, trechos):
    for k, v in flat.items():
        if any(t in k.lower() for t in trechos) and v not in (None, "", "None"):
            return v
    return None


num_re = re.compile(r"300-\d{5}-\d{6}")
email_re = re.compile(r"[\w\.\-+]+@[\w\.\-]+\.\w+")
h2a_re = re.compile(r"h-?2a[^$]{0,100}\$\s?(\d+(?:\.\d+)?)", re.I)


def numero_de(flat, texto):
    m = num_re.search(str(flat.get("caseNumber", ""))) or num_re.search(texto)
    return m.group(0) if m else ""


def salarios(flat):
    if not flat:
        return None, None
    per = str(pega(flat, "jobWagePer") or "").lower()
    try:
        oferta = float(pega(flat, "jobWageOffer"))
    except Exception:
        oferta = None
    if oferta is not None and (oferta <= 0 or "hour" not in per):
        oferta = None
    especial = " ".join(str(pega(flat, s) or "") for s in ["jobSpecialPayInfo", "addSpecialPayInfo"])
    m = h2a_re.search(especial)
    h2a = float(m.group(1)) if m else None
    if h2a is not None and not (7 <= h2a <= 100):
        h2a = None
    return oferta, h2a


# Coordenadas das cidades (para a distância até cidade grande)
nomi = pgeocode.Nominatim("us")
base = getattr(nomi, "_data", None)
if base is None:
    base = nomi._data_frame
base = base[["place_name", "state_code", "latitude", "longitude"]].dropna().copy()
base["nome"] = base["place_name"].str.lower().str.replace(".", "", regex=False).str.strip()
base["uf"] = base["state_code"].str.upper()
g = base.groupby(["nome", "uf"])[["latitude", "longitude"]].mean()
lookup = {idx: (row.latitude, row.longitude) for idx, row in g.iterrows()}


def coordenada(cidade, uf):
    if not cidade or not uf:
        return None
    n = str(cidade).lower().replace(".", "").strip()
    u = str(uf).upper().strip()
    for v in [n, n.replace("st ", "saint "), n.replace("saint ", "st "),
              n.replace("mt ", "mount "), n.replace("ft ", "fort ")]:
        if (v, u) in lookup:
            return lookup[(v, u)]
    return None


gc = geonamescache.GeonamesCache()
cidades = [c for c in gc.get_cities().values()
           if c["countrycode"] == "US" and c["population"] >= POP_MIN]
c_lat = np.array([c["latitude"] for c in cidades])
c_lon = np.array([c["longitude"] for c in cidades])


def milhas(lat1, lon1, lat2, lon2):
    R = 3958.8
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(lon2 - lon1)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


GRUPOS = {
    "gado": ["livestock", "cattle", "cow", "dairy", "ranch", "herd", "beef", "calves", "calf"],
    "colheita": ["harvest", "crop", "fruit", "vegetable", "orchard", "berry", "berries", "picking", "produce"],
    "jardinagem": ["landscap", "grounds", "nursery", "greenhouse", "lawn", "mowing"],
    "agricola geral": ["farmworker", "farm worker", "farm laborer", "agricultur"],
}
HABILIDADE = re.compile(r"driver|mechanic|technician|crew leader|supervisor|manager|operator|beekeeper|welder", re.I)

aceitas = baixar("h2a")
ordens = baixar("jo")

jo_por_num = {}
for rec in ordens:
    flat = achatar(rec)
    n = numero_de(flat, json.dumps(flat))
    if n:
        jo_por_num[n] = flat

linhas = []
for rec in aceitas:
    fh = achatar(rec)
    texto = json.dumps(fh, ensure_ascii=False)
    n = numero_de(fh, texto)
    if not n:
        continue
    jo = jo_por_num.get(n, {})
    titulo = str(pega(jo, "jobTitle") or pega(fh, "jobTitle") or "")
    duties = str(pega(jo, "jobDuties") or pega(fh, "jobDuties") or "")
    base_txt = (titulo + " " + duties).lower()
    tipos = [t for t, ps in GRUPOS.items() if any(p in base_txt for p in ps)]
    if not tipos:
        continue
    ini = pd.to_datetime(pega(jo, "jobBeginDate") or pega(fh, "jobBeginDate"), errors="coerce")
    fim = pd.to_datetime(pega(jo, "jobEndDate") or pega(fh, "jobEndDate"), errors="coerce")
    if pd.notna(fim) and fim.date() < hoje:
        continue
    o1, h1 = salarios(fh)
    o2, h2 = salarios(jo)
    oferta, h2a = (o1 or o2), (h1 or h2)
    cidade = pega(fh, "jobCity") or pega(jo, "jobCity")
    uf = pega(fh, "jobState") or pega(jo, "jobState")
    coord = coordenada(cidade, uf)
    dist, prox = None, None
    if coord:
        d = milhas(coord[0], coord[1], c_lat, c_lon)
        i = int(np.argmin(d))
        dist, prox = round(float(d[i]), 1), f"{cidades[i]['name']}, {cidades[i]['admin1code']}"
    emails = [e for e in email_re.findall(texto + json.dumps(jo)) if "dol.gov" not in e.lower()]
    try:
        trab = int(pega(jo, "jobWrksNeededH2a"))
    except Exception:
        trab = None
    linhas.append({
        "numero_vaga": "H-" + n, "tipo": tipos, "titulo": titulo.strip(),
        "descricao": duties[:8000], "cidade": cidade, "uf": uf,
        "salario_h2a": h2a, "salario_oferta": oferta, "salario_hora": h2a or oferta,
        "cidade_proxima": prox, "dist_milhas": dist,
        "inicio": ini.date().isoformat() if pd.notna(ini) else None,
        "fim": fim.date().isoformat() if pd.notna(fim) else None,
        "trabalhadores": trab, "email": emails[0] if emails else None,
        "telefone": achar(fh, ["phone"]) or achar(jo, ["phone"]),
        "link": f"https://seasonaljobs.dol.gov/jobs/H-{n}",
        "exige_habilidade": bool(HABILIDADE.search(titulo)),
        "atualizado_em": datetime.datetime.utcnow().isoformat() + "Z",
    })

# remove repetidas e grava no Supabase
unicas = {l["numero_vaga"]: l for l in linhas}
linhas = list(unicas.values())
print("Vagas para gravar:", len(linhas))

H = {"apikey": CHAVE, "Content-Type": "application/json", "User-Agent": "h2a-robo/1.0",
     "Prefer": "resolution=merge-duplicates,return=minimal"}
if CHAVE.startswith("eyJ"):
    H["Authorization"] = "Bearer " + CHAVE

for i in range(0, len(linhas), 200):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/vagas?on_conflict=numero_vaga",
                      headers=H, data=json.dumps(linhas[i:i + 200]), timeout=120)
    if r.status_code >= 300:
        raise SystemExit(f"Erro no Supabase {r.status_code}: {r.text[:300]}")

# apaga vagas que terminaram há mais de 30 dias
limite = (hoje - datetime.timedelta(days=30)).isoformat()
requests.delete(f"{SUPABASE_URL}/rest/v1/vagas?fim=lt.{limite}", headers=H, timeout=120)
print("Pronto.")
