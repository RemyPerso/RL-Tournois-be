"""Veille des tournois Rocket League (start.gg + alertes Google) publiée sur GitHub Pages.

Lancé chaque jour par GitHub Actions :
- interroge start.gg (tournois en Belgique et, en option, en ligne) ;
- lit les flux RSS des alertes Google listés dans flux.txt ;
- génère site/index.html et met à jour data/deja_vus.json.

Le token start.gg est lu dans la variable d'environnement STARTGG_TOKEN
(secret du dépôt). Aucune dépendance externe : bibliothèque standard uniquement.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------- Réglages ----------

DOSSIER = Path(__file__).parent
FICHIER_FLUX = DOSSIER / "flux.txt"
ETAT = DOSSIER / "data" / "deja_vus.json"
SORTIE = DOSSIER / "site" / "index.html"

API = "https://api.start.gg/gql/alpha"
FUSEAU = ZoneInfo("Europe/Brussels")
JOURS = 45            # horizon de recherche start.gg, en jours
JOURS_NOUVEAU = 7     # un élément reste « Nouveau » pendant ce nombre de jours
INCLURE_EN_LIGNE = True  # False pour n'afficher que la Belgique

# Tournois en ligne exclus si leur nom contient un de ces termes (mot entier, majuscules ou non)
EXCLURE = ["NA", "OCE", "North America", "Oceania"]

ATOM = "{http://www.w3.org/2005/Atom}"

Q_JEU = """
query($nom: String) {
  videogames(query: {filter: {name: $nom}, perPage: 10}) {
    nodes { id name }
  }
}"""

PAR_PAGE = 25
Q_TOURNOIS = """
query($filtre: TournamentPageFilter, $page: Int, $parPage: Int, $jeu: [ID]!) {
  tournaments(query: {perPage: $parPage, page: $page, sortBy: "startAt asc", filter: $filtre}) {
    nodes {
      id name slug startAt isOnline city countryCode numAttendees
      events(filter: {videogameId: $jeu}) { name type teamRosterSize { minPlayers maxPlayers } prizingInfo }
    }
  }
}"""

# Temporaire : garde un échantillon des épreuves dans data/ pour régler la détection
DIAGNOSTIC = True


# ---------- start.gg ----------

def gql(token, query, variables):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


def id_rocket_league(token):
    noeuds = gql(token, Q_JEU, {"nom": "Rocket League"})["videogames"]["nodes"]
    for n in noeuds:
        if n["name"].lower() == "rocket league":
            return n["id"]
    raise RuntimeError(f"Rocket League introuvable : {noeuds}")


MOTIF_EXCLU = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(re.escape(m) for m in EXCLURE) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def hors_zone(t):
    return bool(MOTIF_EXCLU.search(t["name"]))


# Format repéré dans l'intitulé : « 1v1 », « 2 vs 2 », « 3x3 » ou l'abréviation « 2s ».
# Sans indication, le tournoi est rangé dans les trois formats.
MOTIF_FORMAT = re.compile(r"(?<![0-9])([123])\s*(?:v|vs|x)\s*([123])(?![0-9])", re.IGNORECASE)
MOTIF_FORMAT_S = re.compile(r"(?<![0-9A-Za-z$€£])([123])s\b", re.IGNORECASE)
MOTIF_CASH = re.compile(r"[$€£]|cash ?prize", re.IGNORECASE)


def formats(nom):
    trouves = {a for a, b in MOTIF_FORMAT.findall(nom) if a == b}
    trouves |= set(MOTIF_FORMAT_S.findall(nom))
    return sorted(trouves)


def cash_prize(nom):
    return bool(MOTIF_CASH.search(nom or ""))


def _parcourir(objet, cle=""):
    if isinstance(objet, dict):
        for k, v in objet.items():
            yield from _parcourir(v, k)
    elif isinstance(objet, list):
        for v in objet:
            yield from _parcourir(v, cle)
    else:
        yield cle.lower(), objet


def prix_renseigne(info):
    """Prize money configuré par l'organisateur dans start.gg (prizingInfo, format JSON non documenté)."""
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except ValueError:
            return cash_prize(info)
    if not info:
        return False
    paires = list(_parcourir(info))
    if any(("enable" in k or "active" in k) and v is False for k, v in paires):
        return False
    if any(isinstance(v, str) and cash_prize(v) for _, v in paires):
        return True
    return any(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
               and any(m in k for m in ("amount", "value", "percent", "prize", "payout"))
               for k, v in paires)


def epreuves(t):
    return t.get("events") or []


def formats_tournoi(t):
    """Formats d'après l'intitulé du tournoi, puis chaque épreuve : son nom, sinon la taille des équipes."""
    trouves = set(formats(t["name"]))
    for e in epreuves(t):
        f = set(formats(e.get("name") or ""))
        if not f:
            taille = (e.get("teamRosterSize") or {}).get("minPlayers")
            if taille in (1, 2, 3):
                f = {str(taille)}
            elif not e.get("teamRosterSize") and e.get("type") == 1:  # épreuve individuelle
                f = {"1"}
        trouves |= f
    return sorted(trouves)


def cash_tournoi(t):
    return cash_prize(t["name"]) or any(
        cash_prize(e.get("name")) or prix_renseigne(e.get("prizingInfo")) for e in epreuves(t))


def tournois(token, jeu_id, filtre_extra, pages=4):
    debut = int(time.time())
    filtre = {
        "upcoming": True,
        "videogameIds": [jeu_id],
        "afterDate": debut,
        "beforeDate": debut + JOURS * 86400,
        **filtre_extra,
    }
    resultats = []
    for page in range(1, pages + 1):
        variables = {"filtre": filtre, "page": page, "parPage": PAR_PAGE, "jeu": [jeu_id]}
        noeuds = gql(token, Q_TOURNOIS, variables)["tournaments"]["nodes"] or []
        resultats += noeuds
        if len(noeuds) < PAR_PAGE:
            break
    return resultats


# ---------- Alertes Google (flux Atom) ----------

def lire_liste_flux():
    if not FICHIER_FLUX.exists():
        return []
    lignes = FICHIER_FLUX.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lignes if l.strip() and not l.strip().startswith("#")]


def nettoyer(texte):
    return html.unescape(re.sub(r"<[^>]+>", "", texte or "")).strip()


PARAMS_PUB = re.compile(r"^(utm_|fbclid$|gclid$|ocid$|xtor$|at_)", re.IGNORECASE)


def normaliser_url(url):
    """Même article = même clé, même si l'adresse varie un peu d'un flux à l'autre."""
    u = urllib.parse.urlparse(url)
    hote = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    chemin = re.sub(r"/(amp/?)?$", "", u.path) or "/"
    params = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if not PARAMS_PUB.match(k)]
    requete = urllib.parse.urlencode(sorted(params))
    return f"{hote}{chemin}" + (f"?{requete}" if requete else "")


def normaliser_titre(titre):
    titre = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", titre)  # retire « - Nom du site » en fin de titre
    return re.sub(r"[^a-z0-9à-ÿ]+", " ", titre.lower()).strip()


def lire_flux(contenu):
    resultats = []
    for e in ET.fromstring(contenu).iter(f"{ATOM}entry"):
        lien = e.find(f"{ATOM}link").get("href")
        # Google enveloppe les liens dans une redirection : on récupère l'URL réelle
        vrai = urllib.parse.parse_qs(urllib.parse.urlparse(lien).query).get("url", [lien])[0]
        resultats.append({
            "id": "alerte:" + normaliser_url(vrai),
            "titre": nettoyer(e.findtext(f"{ATOM}title")),
            "url": vrai,
            "date": (e.findtext(f"{ATOM}published") or "")[:10],
            "source": urllib.parse.urlparse(vrai).netloc,
        })
    return resultats


def dedoublonner(liste):
    """Un article remonté par plusieurs alertes n'apparaît qu'une fois :
    même adresse (après nettoyage) ou même titre."""
    par_id, titres_vus, resultat = set(), set(), []
    for a in sorted(liste, key=lambda a: a["date"]):  # on garde la première apparition
        titre = normaliser_titre(a["titre"])
        if a["id"] in par_id or (titre and titre in titres_vus):
            continue
        par_id.add(a["id"])
        titres_vus.add(titre)
        resultat.append(a)
    return sorted(resultat, key=lambda a: a["date"], reverse=True)


def alertes():
    tout = []
    for flux in lire_liste_flux():
        try:
            with urllib.request.urlopen(flux, timeout=30) as r:
                tout += lire_flux(r.read())
        except Exception as exc:  # un flux en panne ne bloque pas le reste
            print(f"::warning::Flux en erreur : {flux} ({exc})")
    return dedoublonner(tout)


# ---------- Mémoire des éléments déjà vus ----------

def charger_etat():
    if not ETAT.exists():
        return None
    return json.loads(ETAT.read_text(encoding="utf-8"))


def mettre_a_jour_etat(etat, identifiants, aujourdhui):
    """Retourne (nouvel état, ensemble des identifiants à marquer « Nouveau »)."""
    premier_passage = etat is None
    vus = dict((etat or {}).get("vus", {}))
    limite = (aujourdhui - timedelta(days=JOURS_NOUVEAU)).isoformat()
    # Au tout premier passage, rien n'est « nouveau » : tout est découvert d'un coup,
    # donc on l'antidate pour qu'il ne soit pas marqué les jours suivants non plus.
    date_ajout = limite if premier_passage else aujourdhui.isoformat()
    for i in identifiants:
        vus.setdefault(i, date_ajout)
    nouveaux = {i for i in identifiants if vus[i] > limite}
    # On oublie ce qui n'est plus affiché depuis longtemps pour garder le fichier léger
    oubli = (aujourdhui - timedelta(days=120)).isoformat()
    vus = {i: d for i, d in vus.items() if i in identifiants or d > oubli}
    nouvel_etat = {"derniere_execution": datetime.now(FUSEAU).isoformat(timespec="minutes"),
                   "vus": dict(sorted(vus.items()))}
    return nouvel_etat, nouveaux


# ---------- Page HTML ----------

MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
        "juil.", "août", "sept.", "oct.", "nov.", "déc."]

CSS = """
:root { --fond:#0b1119; --carte:#141d29; --survol:#1b2737; --bord:#22303f;
        --texte:#e8eef5; --doux:#8797ab; --bleu:#2f8cff; --orange:#ff8a1f; --vert:#22c3a6; }
* { box-sizing: border-box; }
body { margin:0; background:var(--fond); color:var(--texte);
       font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.titre { font-family: "Rajdhani", system-ui, sans-serif; }
header { background: linear-gradient(135deg, #10233d 0%, #0b1119 70%);
         border-bottom:1px solid var(--bord); padding:28px 16px 22px; }
.cadre { max-width:960px; margin:0 auto; }
h1 { font-size:32px; font-weight:700; letter-spacing:.5px; margin:0; text-transform:uppercase; }
h1 span { color:var(--bleu); }
.maj { color:var(--doux); font-size:13px; margin-top:4px; }
.tuiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); gap:10px; margin-top:18px; }
.tuile { background:var(--carte); border:1px solid var(--bord); border-radius:10px; padding:12px 14px; }
.tuile .n { font-size:30px; font-weight:700; line-height:1; }
.tuile .l { color:var(--doux); font-size:12px; text-transform:uppercase; letter-spacing:.8px; margin-top:4px; }
.tuile.chaude { border-color:var(--orange); }
.tuile.chaude .n { color:var(--orange); }
main { padding:8px 16px 40px; }
section { margin-top:26px; }
h2 { font-size:19px; text-transform:uppercase; letter-spacing:1px; margin:0 0 10px; padding-left:10px;
     border-left:3px solid var(--bleu); display:flex; align-items:center; gap:8px; }
.compte { font-family: system-ui, sans-serif; background:var(--carte); border:1px solid var(--bord);
          color:var(--doux); font-size:12px; border-radius:999px; padding:1px 8px; letter-spacing:0; }
.carte { display:flex; align-items:center; gap:10px; background:var(--carte); border:1px solid var(--bord);
         border-radius:10px; padding:10px 10px 10px 14px; margin-bottom:8px;
         transition: background .15s, border-color .15s; }
.carte:hover { background:var(--survol); border-color:var(--bleu); }
.carte[hidden] { display:none; }
.lien { flex:1; min-width:0; display:flex; align-items:center; gap:14px; color:inherit; text-decoration:none; }
.lien:focus-visible { outline:2px solid var(--bleu); outline-offset:4px; border-radius:6px; }
.date { flex:0 0 52px; text-align:center; background:var(--fond); border-radius:8px; padding:6px 0; }
.jour { display:block; font-size:22px; font-weight:700; line-height:1; }
.mois { display:block; color:var(--doux); font-size:11px; text-transform:uppercase; }
.infos { flex:1; min-width:0; }
.nom { font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.meta { color:var(--doux); font-size:13px; margin-top:2px; }
.tags { display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
.tag { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.6px; padding:3px 8px; border-radius:6px; }
.nouveau { background:rgba(255,138,31,.15); color:var(--orange); }
.ligne { background:rgba(34,195,166,.12); color:var(--vert); }
.be { background:rgba(47,140,255,.15); color:var(--bleu); }
.format { background:rgba(232,238,245,.08); color:var(--texte); }
.cash { background:rgba(255,200,61,.14); color:#ffc83d; }
.carte.filtre-off { display:none; }
.filtres { position:sticky; top:0; z-index:2; display:flex; flex-wrap:wrap; align-items:center; gap:8px;
           margin:26px -16px 0; padding:10px 16px; background:rgba(11,17,25,.94); border-bottom:1px solid var(--bord); }
.filtres .groupe { display:flex; background:var(--carte); border:1px solid var(--bord); border-radius:8px; padding:3px; }
.filtres button { background:none; border:none; color:var(--doux); font:inherit; font-size:13px; font-weight:700;
                  padding:6px 12px; border-radius:6px; cursor:pointer; }
.filtres .groupe button[aria-pressed=true] { background:var(--bleu); color:#fff; }
.filtres .bouton-cash { border:1px solid var(--bord); border-radius:8px; padding:8px 12px; }
.filtres .bouton-cash[aria-pressed=true] { color:#ffc83d; border-color:#ffc83d; background:rgba(255,200,61,.12); }
.filtres button:focus-visible { outline:2px solid var(--bleu); outline-offset:2px; }
.filtres .aide { color:var(--doux); font-size:12px; margin-left:auto; }
.actions { display:flex; align-items:center; gap:4px; }
.coche { display:flex; align-items:center; gap:6px; font-size:12px; font-weight:600; color:var(--doux);
         border:1px solid var(--bord); border-radius:6px; padding:5px 9px; cursor:pointer; user-select:none; white-space:nowrap; }
.coche input { accent-color:var(--vert); margin:0; width:15px; height:15px; cursor:pointer; }
.coche:hover { border-color:var(--vert); }
.coche:has(input:checked) { color:var(--vert); border-color:var(--vert); background:rgba(34,195,166,.12); }
.effacer { background:none; border:1px solid transparent; color:var(--doux); font-size:22px; line-height:1;
           width:34px; height:34px; border-radius:6px; cursor:pointer; flex:0 0 auto; }
.effacer:hover, .effacer:focus-visible { color:#ff6b6b; border-color:var(--bord); }
.inscriptions h2 { border-left-color:var(--vert); }
.inscriptions .nouveau { display:none; }
.vide { color:var(--doux); font-style:italic; }
.masques { margin-top:24px; text-align:center; font-size:13px; color:var(--doux); }
.masques button, #annuler button { background:none; border:none; padding:0; font:inherit; cursor:pointer; color:var(--bleu); }
.masques button { text-decoration:underline; }
#annuler { position:fixed; left:50%; bottom:16px; transform:translateX(-50%); z-index:5; display:flex; gap:14px;
           align-items:center; background:var(--survol); border:1px solid var(--bord); border-radius:10px;
           padding:10px 16px; font-size:14px; box-shadow:0 8px 24px rgba(0,0,0,.45); white-space:nowrap; }
#annuler[hidden] { display:none; }
#annuler button { font-weight:700; }
footer { color:var(--doux); font-size:12px; text-align:center; padding:0 16px 28px; }
footer a { color:var(--doux); }
@media (max-width:560px) {
  h1 { font-size:26px; }
  .carte { flex-wrap:wrap; row-gap:8px; }
  .lien { flex-basis:100%; }
  .nom { white-space:normal; }
  .tags { justify-content:flex-start; margin-left:66px; }
  .actions { margin-left:auto; }
  .filtres .aide { display:none; }
}
"""


def bloc_date(dt):
    if not dt:
        return "<div class='date'><span class='jour titre'>?</span></div>"
    return (f"<div class='date'><span class='jour titre'>{dt.day:02d}</span>"
            f"<span class='mois'>{MOIS[dt.month - 1]}</span></div>")


def attr(valeur):
    return html.escape(str(valeur), quote=True)


BOUTON_EFFACER = ("<button type='button' class='effacer' aria-label='Masquer' "
                  "title='Masquer définitivement'>×</button>")


def carte_tournoi(t, nouveaux):
    dt = datetime.fromtimestamp(t["startAt"], FUSEAU) if t.get("startAt") else None
    lieu = "En ligne" if t.get("isOnline") else (t.get("city") or "Belgique")
    inscrits = t.get("numAttendees")
    meta = " · ".join(x for x in [
        dt.strftime("%H:%M") if dt else "",
        lieu,
        f"{inscrits} inscrits" if inscrits else "",
    ] if x)
    fmts, cash = formats_tournoi(t), cash_tournoi(t)
    tags = "<span class='tag nouveau'>Nouveau</span>" if str(t["id"]) in nouveaux else ""
    tags += "".join(f"<span class='tag format'>{f}v{f}</span>" for f in fmts)
    tags += "<span class='tag cash'>Cash prize</span>" if cash else ""
    tags += "<span class='tag ligne'>En ligne</span>" if t.get("isOnline") else "<span class='tag be'>BE</span>"
    return (f"<div class='carte' data-id='{attr(t['id'])}' data-ts='{t.get('startAt') or 0}' "
            f"data-formats='{' '.join(fmts) or '1 2 3'}' data-cash='{1 if cash else 0}'>"
            f"<a class='lien' href='https://www.start.gg/{attr(t['slug'])}'>{bloc_date(dt)}"
            f"<div class='infos'><div class='nom'>{html.escape(t['name'])}</div>"
            f"<div class='meta'>{html.escape(meta)}</div></div></a>"
            f"<div class='tags'>{tags}</div>"
            f"<div class='actions'><label class='coche'><input type='checkbox' class='inscrit'> Inscrit</label>"
            f"{BOUTON_EFFACER}</div></div>")


def carte_alerte(a, nouveaux):
    try:
        dt = datetime.strptime(a["date"], "%Y-%m-%d")
    except ValueError:
        dt = None
    tags = "<span class='tag nouveau'>Nouveau</span>" if a["id"] in nouveaux else ""
    return (f"<div class='carte' data-id='{attr(a['id'])}'>"
            f"<a class='lien' href='{attr(a['url'])}'>{bloc_date(dt)}"
            f"<div class='infos'><div class='nom'>{html.escape(a['titre'])}</div>"
            f"<div class='meta'>{html.escape(a['source'])}</div></div></a>"
            f"<div class='tags'>{tags}</div><div class='actions'>{BOUTON_EFFACER}</div></div>")


def section(cle, titre, cartes, vide, classe=""):
    return (f"<section data-section='{cle}'{f' class={classe!r}' if classe else ''}>"
            f"<h2 class='titre'>{titre} <span class='compte'>{len(cartes)}</span></h2>"
            f"<div class='liste'>{''.join(cartes)}</div>"
            f"<p class='vide'{' hidden' if cartes else ''}>{vide}</p></section>")


# Inscriptions et masquages : mémorisés dans le navigateur (localStorage), la page restant statique.
SCRIPT = """
(() => {
  const CLE_I = "rl-inscrits", CLE_M = "rl-masques", CLE_F = "rl-filtres";
  const lire = (k) => { try { return JSON.parse(localStorage.getItem(k)) || {}; } catch (e) { return {}; } };
  const ecrire = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };
  const aujourdhui = () => new Date().toISOString().slice(0, 10);
  let inscrits = lire(CLE_I), masques = lire(CLE_M);
  const filtres = Object.assign({ format: "1", cash: false }, lire(CLE_F));
  if (!["1", "2", "3", "tous"].includes(filtres.format)) filtres.format = "1";

  // Nettoyage : tournois terminés (plus de 24 h après le début) et masquages de plus de 6 mois
  const maintenant = Date.now() / 1000;
  for (const [id, t] of Object.entries(inscrits)) if (t.ts && t.ts < maintenant - 86400) delete inscrits[id];
  const limite = new Date(Date.now() - 180 * 86400000).toISOString().slice(0, 10);
  for (const [id, d] of Object.entries(masques)) if (d < limite) delete masques[id];

  const listeInscrits = document.querySelector("[data-section=inscrits] .liste");
  const cartes = [...document.querySelectorAll("main .carte")];
  const origine = new Map(cartes.map((c) => [c, c.parentElement]));
  const presents = new Set(cartes.map((c) => c.dataset.id));

  // Les tournois cochés encore présents sur start.gg : on rafraîchit leur copie mémorisée
  for (const c of cartes) if (c.dataset.id in inscrits) inscrits[c.dataset.id] = { ts: +c.dataset.ts || 0, html: c.outerHTML };
  ecrire(CLE_I, inscrits); ecrire(CLE_M, masques);

  function afficher() {
    document.querySelectorAll(".carte.copie").forEach((c) => c.remove());
    for (const c of cartes) {
      const id = c.dataset.id, inscrit = id in inscrits, masque = id in masques;
      c.hidden = masque;
      const coche = c.querySelector(".inscrit");
      if (coche) coche.checked = inscrit;
      (inscrit && !masque ? listeInscrits : origine.get(c)).append(c);
    }
    // Tournois cochés qui ne sont plus listés (inscriptions closes) : on les garde grâce à leur copie
    for (const [id, t] of Object.entries(inscrits)) {
      if (presents.has(id) || id in masques || !t.html) continue;
      const modele = document.createElement("template");
      modele.innerHTML = t.html.trim();
      const c = modele.content.firstElementChild;
      if (!c) continue;
      c.classList.add("copie"); c.hidden = false;
      c.querySelector(".inscrit").checked = true;
      listeInscrits.append(c);
    }
    [...listeInscrits.children].sort((a, b) => (+a.dataset.ts || 0) - (+b.dataset.ts || 0))
      .forEach((c) => listeInscrits.append(c));

    // Filtres : uniquement sur les tournois des sections Belgique et En ligne
    for (const c of document.querySelectorAll("section[data-section=be] .carte, section[data-section=ligne] .carte")) {
      const fmts = (c.dataset.formats || "1 2 3").split(" ");
      const ok = (filtres.format === "tous" || fmts.includes(filtres.format)) && (!filtres.cash || c.dataset.cash === "1");
      c.classList.toggle("filtre-off", !ok);
    }
    listeInscrits.querySelectorAll(".filtre-off").forEach((c) => c.classList.remove("filtre-off"));
    document.querySelectorAll(".filtres .groupe button").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.format === filtres.format)));
    document.querySelector(".bouton-cash").setAttribute("aria-pressed", String(filtres.cash));

    for (const s of document.querySelectorAll("section[data-section]")) {
      const n = s.querySelectorAll(".liste > .carte:not([hidden]):not(.filtre-off)").length;
      const masquesParFiltre = s.querySelectorAll(".liste > .carte.filtre-off:not([hidden])").length;
      s.querySelector(".compte").textContent = n;
      const vide = s.querySelector(".vide");
      vide.dataset.defaut = vide.dataset.defaut || vide.textContent;
      vide.textContent = masquesParFiltre ? "Aucun tournoi ne correspond aux filtres ici." : vide.dataset.defaut;
      vide.hidden = n > 0;
      const tuile = document.querySelector(`.tuile[data-compte="${s.dataset.section}"] .n`);
      if (tuile) tuile.textContent = n;
    }
    const nouveaux = document.querySelectorAll(
      "section:not([data-section=inscrits]) .carte:not([hidden]):not(.filtre-off) .nouveau").length;
    const tuileN = document.querySelector(".tuile[data-compte=nouveaux]");
    if (tuileN) { tuileN.querySelector(".n").textContent = nouveaux; tuileN.classList.toggle("chaude", nouveaux > 0); }

    const nMasques = cartes.filter((c) => c.hidden).length;
    const zone = document.getElementById("masques");
    zone.hidden = nMasques === 0;
    zone.querySelector("span").textContent =
      nMasques + (nMasques > 1 ? " éléments masqués" : " élément masqué");
  }

  document.addEventListener("change", (e) => {
    if (!e.target.matches(".carte .inscrit")) return;
    const c = e.target.closest(".carte"), id = c.dataset.id;
    if (e.target.checked) inscrits[id] = { ts: +c.dataset.ts || 0, html: c.outerHTML };
    else delete inscrits[id];
    ecrire(CLE_I, inscrits);
    afficher();
  });

  const barre = document.getElementById("annuler");
  let dernier = null, minuteur = null;
  document.addEventListener("click", (e) => {
    const bouton = e.target.closest(".effacer");
    if (!bouton) return;
    const id = bouton.closest(".carte").dataset.id;
    dernier = { id, inscrit: inscrits[id] };
    masques[id] = aujourdhui();
    delete inscrits[id];
    ecrire(CLE_M, masques); ecrire(CLE_I, inscrits);
    afficher();
    barre.hidden = false;
    clearTimeout(minuteur);
    minuteur = setTimeout(() => { barre.hidden = true; }, 7000);
  });
  barre.querySelector("button").addEventListener("click", () => {
    if (!dernier) return;
    delete masques[dernier.id];
    if (dernier.inscrit) inscrits[dernier.id] = dernier.inscrit;
    ecrire(CLE_M, masques); ecrire(CLE_I, inscrits);
    dernier = null; barre.hidden = true;
    afficher();
  });
  document.querySelector(".filtres").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.format) filtres.format = b.dataset.format;
    else filtres.cash = !filtres.cash;
    ecrire(CLE_F, filtres);
    afficher();
  });
  document.querySelector("#masques button").addEventListener("click", () => {
    for (const c of cartes) delete masques[c.dataset.id];
    ecrire(CLE_M, masques);
    afficher();
  });

  afficher();
})();
"""


def ecrire_page(belges, en_ligne, liste_alertes, nouveaux):
    maj = datetime.now(FUSEAU).strftime("%d/%m/%Y à %H:%M")
    stats = [("be", "Belgique", len(belges))]
    if INCLURE_EN_LIGNE:
        stats.append(("ligne", "En ligne", len(en_ligne)))
    stats += [("alertes", "Alertes", len(liste_alertes)), ("nouveaux", "Nouveaux", len(nouveaux))]
    tuiles = "".join(
        f"<div class='tuile{' chaude' if cle == 'nouveaux' and n else ''}' data-compte='{cle}'>"
        f"<div class='n titre'>{n}</div><div class='l'>{nom}</div></div>"
        for cle, nom, n in stats
    )
    sections = [
        section("inscrits", "Mes inscriptions", [],
                "Coche « Inscrit » sur un tournoi pour le retrouver ici.", "inscriptions"),
        section("be", "En Belgique", [carte_tournoi(t, nouveaux) for t in belges],
                "Aucun tournoi trouvé en Belgique pour l'instant."),
    ]
    if INCLURE_EN_LIGNE:
        sections.append(section("ligne", "En ligne · inscriptions ouvertes",
                                [carte_tournoi(t, nouveaux) for t in en_ligne],
                                "Aucun tournoi en ligne trouvé."))
    sections.append(section("alertes", "Actus (alertes Google)",
                            [carte_alerte(a, nouveaux) for a in liste_alertes],
                            "Rien dans les flux pour l'instant."))

    SORTIE.parent.mkdir(parents=True, exist_ok=True)
    SORTIE.write_text(f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0b1119">
<title>Tournois Rocket League · Belgique</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@600;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
<header><div class="cadre">
  <h1 class="titre">Tournois <span>Rocket League</span></h1>
  <div class="maj">Mis à jour le {maj} · « Nouveau » = apparu ces {JOURS_NOUVEAU} derniers jours</div>
  <div class="tuiles">{tuiles}</div>
</div></header>
<main><div class="cadre">
{sections[0]}
<div class="filtres" role="toolbar" aria-label="Filtres des tournois">
  <div class="groupe" role="group" aria-label="Format">
    <button type="button" data-format="1">1v1</button><button type="button" data-format="2">2v2</button><button type="button" data-format="3">3v3</button><button type="button" data-format="tous">Tous</button>
  </div>
  <button type="button" class="bouton-cash" aria-pressed="false">Cash prize</button>
  <span class="aide">Sans format indiqué, un tournoi apparaît dans les trois.</span>
</div>
{''.join(sections[1:])}
<p class="masques" id="masques" hidden><span></span> · <button type="button">Tout réafficher</button></p>
</div></main>
<div id="annuler" role="status" hidden><span>Masqué.</span><button type="button">Annuler</button></div>
<footer>Données : <a href="https://www.start.gg">start.gg</a> et Google Alerts · mise à jour automatique chaque jour<br>
Les inscriptions cochées et les éléments masqués sont mémorisés dans ce navigateur.</footer>
<script>{SCRIPT}</script>
</body></html>
""", encoding="utf-8")


# ---------- Programme principal ----------

def main():
    token = os.environ.get("STARTGG_TOKEN", "").strip()
    if not token:
        sys.exit("Variable STARTGG_TOKEN absente : ajoute le secret dans les réglages du dépôt.")

    jeu_id = id_rocket_league(token)
    belges = tournois(token, jeu_id, {"countryCode": "BE"})
    en_ligne = []
    if INCLURE_EN_LIGNE:
        deja = {t["id"] for t in belges}
        en_ligne = [t for t in tournois(token, jeu_id, {"regOpen": True}, pages=6)
                    if t.get("isOnline") and not hors_zone(t) and t["id"] not in deja]
    liste_alertes = alertes()

    identifiants = {str(t["id"]) for t in belges + en_ligne} | {a["id"] for a in liste_alertes}
    etat, nouveaux = mettre_a_jour_etat(charger_etat(), identifiants, date.today())

    ecrire_page(belges, en_ligne, liste_alertes, nouveaux)
    if DIAGNOSTIC:
        echantillon = [{"nom": t["name"], "formats": formats_tournoi(t), "cash": cash_tournoi(t),
                        "epreuves": epreuves(t)} for t in belges + en_ligne]
        (DOSSIER / "data" / "diagnostic_epreuves.json").write_text(
            json.dumps(echantillon, ensure_ascii=False, indent=1), encoding="utf-8")
    ETAT.parent.mkdir(parents=True, exist_ok=True)
    ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(belges)} en Belgique, {len(en_ligne)} en ligne, "
          f"{len(liste_alertes)} alertes, {len(nouveaux)} nouveaux.")


if __name__ == "__main__":
    main()
