# app.py
# ─────────────────────────────────────────────────────────────────────────────
# Suivi d’avancement — Saisie hebdomadaire (Streamlit + Supabase)
# - Auth email/mot de passe
# - Sélection de projet
# - Saisie des % + commentaires + (optionnel) date PV
# - Upload PV (PDF/DOCX/DOC) dans Storage :
#     pv-chantier/<UUID_projet>/<YYYYMMDD>/<uuid>_<nom_fichier>
# - Historique des PV (liens signés 1 h)
# - Reset des champs lorsque le projet change
# - Compatible Streamlit 1.38 (pas de experimental_rerun, pas de set_state post-widget)
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import os
import socket
import platform
import uuid
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import streamlit as st

# ── Constantes appli ─────────────────────────────────────────────────────────
BUCKET_PV = "pv-chantier"
MAX_UPLOAD_MB = 25
ALLOWED_EXT = {".pdf", ".docx", ".doc"}
SIGNED_URL_TTL = 3600  # 1 heure

APP_TITLE = "Suivi d’avancement — Saisie"
PAGE_ICON = "🧭"

# ── Supabase (sync client v2) ────────────────────────────────────────────────
# Dépendances : supabase==2.4.0 (déjà dans tes requirements)

from supabase import create_client, Client


@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        st.error("Variables d’environnement SUPABASE_URL / SUPABASE_ANON_KEY manquantes.")
        st.stop()
    try:
        return create_client(url, key)
    except Exception as e:
        st.error(f"Erreur création client Supabase : {e}")
        st.stop()


def dns_ping_from_url(url: str) -> Optional[str]:
    try:
        host = url.replace("https://", "").replace("http://", "").split("/")[0]
        ip = socket.gethostbyname(host)
        return ip
    except Exception:
        return None


# ── Session state par défaut (place ici, avant tout usage de st.session_state) ─
for k, v in {
    "user": None,                        # objet utilisateur gotrue ou None
    "selected_project_id": None,         # UUID projet
    "uploader_version": 0,               # pour reset file_uploader quand projet change
    # valeurs de saisie
    "form_progress_travaux": 0.0,
    "form_progress_paiements": 0.0,
    "form_commentaires": "",
    "form_date_pv": None,                # date | None
}.items():
    st.session_state.setdefault(k, v)

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title=APP_TITLE, page_icon=PAGE_ICON, layout="wide")


# ── Fonctions métier ─────────────────────────────────────────────────────────
def list_projects(sb: Client) -> List[Dict]:
    """Retourne [{id,name}] triés par nom."""
    try:
        res = sb.table("projects").select("id,name").order("name").execute()
        return res.data or []
    except Exception as e:
        st.warning(f"Erreur lecture projets : {e}")
        return []


def insert_update(
    sb: Client,
    project_id: str,
    progress_t: float,
    progress_p: float,
    commentaires: str,
    pv_date: Optional[date],
) -> bool:
    payload = {
        "project_id": project_id,
        "progress_travaux": progress_t,
        "progress_paiements": progress_p,
        "commentaires": commentaires,
    }
    if pv_date is not None:
        payload["pv_chantier"] = pv_date.isoformat()
    try:
        sb.table("project_updates").insert(payload).execute()
        return True
    except Exception as e:
        st.error(f"Erreur enregistrement mise à jour : {e}")
        return False


def _bytes_from_uploader(uploaded_files) -> List[Tuple[str, bytes]]:
    """Transforme les UploadedFile streamlit en [(filename, bytes)]."""
    if not uploaded_files:
        return []
    out: List[Tuple[str, bytes]] = []
    for uf in uploaded_files:
        try:
            content = uf.read()
            out.append((uf.name, content))
        except Exception as e:
            st.warning(f"{uf.name}: lecture impossible ({e})")
    return out


def upload_files(sb: Client, project_id: str, files: List[Tuple[str, bytes]]) -> Tuple[int, List[str]]:
    """
    Upload des fichiers dans:
        pv-chantier/<project_id>/<YYYYMMDD>/<uuid>_<fname>
    Retourne (nb_ok, warnings)
    """
    ok = 0
    warns: List[str] = []
    today_str = datetime.utcnow().strftime("%Y%m%d")
    base_path = f"{project_id}/{today_str}"  # ✅ pas de duplication de dossier

    for fname, content in files:
        ext = os.path.splitext(fname)[1].lower()
        if ext not in ALLOWED_EXT:
            warns.append(f"{fname}: extension non autorisée.")
            continue
        if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
            warns.append(f"{fname}: dépasse {MAX_UPLOAD_MB} Mo.")
            continue
        try:
            path = f"{base_path}/{uuid.uuid4().hex}_{fname}"
            sb.storage.from_(BUCKET_PV).upload(
                path=path,
                file=content,
                file_options={"contentType": "application/octet-stream"},
            )
            ok += 1
        except Exception as e:
            warns.append(f"{fname}: {e}")
    return ok, warns


def list_signed_pv(sb: Client, project_id: str, limit_per_day: int = 500) -> List[Dict]:
    """
    Récupère l’historique des PV sous forme de liens signés:
    - parcours des dossiers dates sous <project_id>/
    - pour chaque fichier : create_signed_url
    Retourne: [{file_name, url, uploaded_at}]
    """
    results: List[Dict] = []
    try:
        # Liste des sous-dossiers (dates) sous <project_id>/
        days = sb.storage.from_(BUCKET_PV).list(path=project_id)
        for day in days or []:
            if day.get("id") or not day.get("name"):
                # les dossiers ont is_folder=True (SDK v2 expose 'id' vide pour dossier),
                # mais sur certaines versions, on filtre juste par 'name' et 'metadata':
                pass
            day_name = day["name"]
            # liste fichiers dans <project_id>/<YYYYMMDD>
            files = sb.storage.from_(BUCKET_PV).list(path=f"{project_id}/{day_name}", limit=limit_per_day)
            for f in files or []:
                if f.get("name"):
                    full_path = f"{project_id}/{day_name}/{f['name']}"
                    try:
                        signed = sb.storage.from_(BUCKET_PV).create_signed_url(full_path, SIGNED_URL_TTL)
                        results.append(
                            {
                                "file_name": f["name"],
                                "url": signed["signedURL"] if isinstance(signed, dict) else signed,
                                "uploaded_at": f.get("created_at") or f.get("updated_at") or "",
                            }
                        )
                    except Exception:
                        # Ignore un fichier qui poserait problème pour ne pas casser l’affichage
                        continue
    except Exception as e:
        st.warning(f"Erreur lecture Storage : {e}")
        return []
    # tri inverse (du plus récent au plus ancien)
    results.sort(key=lambda x: x["uploaded_at"], reverse=True)
    return results


# ── UI : composants ──────────────────────────────────────────────────────────
def auth_panel(sb: Client):
    """Affiche le bloc de connexion si user absent, sinon info + bouton déconnexion."""
    user = st.session_state["user"]
    if user:
        st.caption(f"Connecté : {user.get('email', '')}")
        if st.button("Se déconnecter", type="secondary"):
            try:
                sb.auth.sign_out()
            except Exception:
                pass
            st.session_state["user"] = None
            # reset de la sélection projet et des champs
            _reset_form_state(reset_project=True)
            st.rerun()
        return

    st.subheader("Connexion")
    email = st.text_input("Email", value="", key="auth_email")
    pwd = st.text_input("Mot de passe", type="password", value="", key="auth_pwd")
    colb1, colb2 = st.columns([1, 1])
    with colb1:
        if st.button("Connexion", type="primary", use_container_width=False):
            try:
                res = sb.auth.sign_in_with_password({"email": email, "password": pwd})
                st.session_state["user"] = {"email": res.user.email, "id": res.user.id}
                st.success("Connexion réussie.")
                st.rerun()
            except Exception as e:
                st.error(f"Échec de connexion : {e}")
    with colb2:
        if st.button("Se déconnecter", type="secondary"):
            try:
                sb.auth.sign_out()
            except Exception:
                pass
            st.session_state["user"] = None
            _reset_form_state(reset_project=True)
            st.rerun()


def _reset_form_state(reset_project: bool = False):
    """Réinitialise les champs de saisie (et éventuellement le projet)."""
    if reset_project:
        st.session_state["selected_project_id"] = None
    st.session_state["uploader_version"] += 1  # force reset du file_uploader
    st.session_state["form_progress_travaux"] = 0.0
    st.session_state["form_progress_paiements"] = 0.0
    st.session_state["form_commentaires"] = ""
    st.session_state["form_date_pv"] = None


def seleccionar_projet(sb: Client) -> Optional[str]:
    """Selectbox des projets. Reset des champs si changement."""
    projects = list_projects(sb)
    if not projects:
        st.info("Aucun projet trouvé dans la table **projects**.")
        return None

    id_to_name = {p["id"]: p["name"] for p in projects}
    names = [id_to_name[p["id"]] for p in projects]

    # Déterminer valeur courante (id)
    current_id = st.session_state.get("selected_project_id", None)
    # index sélectionné
    index = 0
    if current_id and current_id in id_to_name:
        # trouver l’index correspondant
        for i, p in enumerate(projects):
            if p["id"] == current_id:
                index = i
                break

    choice = st.selectbox("Projet", options=names, index=index if names else 0, key="project_selectbox")
    # Retrouver l’id à partir du nom choisi
    new_id = None
    for p in projects:
        if p["name"] == choice:
            new_id = p["id"]
            break

    # Reset si changement de projet
    if new_id != current_id:
        st.session_state["selected_project_id"] = new_id
        _reset_form_state(reset_project=False)
        # Re-run pour rafraîchir proprement
        st.rerun()

    return st.session_state["selected_project_id"]


def form_panel(sb: Client, project_id: Optional[str]):
    """Bloc de saisie + upload + historique, affiché uniquement si connecté & projet choisi."""
    if not project_id:
        return

    st.subheader("Nouvelle mise à jour")

    col1, col2 = st.columns(2)
    with col1:
        st.number_input(
            "Progression travaux (%)",
            min_value=0.0,
            max_value=100.0,
            step=1.0,
            key="form_progress_travaux",
            help="Valeur entière ou décimale.",
        )
    with col2:
        st.number_input(
            "Progression paiements (%)",
            min_value=0.0,
            max_value=100.0,
            step=1.0,
            key="form_progress_paiements",
        )

    # Date PV (optionnelle) — on ne modifie pas la session après instanciation
    pv_date_value: Optional[date] = st.session_state.get("form_date_pv", None)
    # Streamlit 1.38 n’autorise pas None en value => on utilise un checkbox pour activer la date
    use_pv_date = st.checkbox("Renseigner une date de PV de chantier", value=pv_date_value is not None)
    if use_pv_date:
        # Si pas encore définie, on propose aujourd’hui par défaut mais sans toucher la session
        default_date = pv_date_value or date.today()
        pv_date = st.date_input("Date du PV de chantier (optionnel)", value=default_date, key="form_date_pv")
    else:
        # Si décoché, on annule la date en session
        st.session_state["form_date_pv"] = None

    st.text_area(
        "Commentaires",
        key="form_commentaires",
        placeholder="Observations, risques, points bloquants…",
        height=120,
    )

    st.markdown("### Joindre le PV (PDF/DOCX/DOC)")
    uploaded = st.file_uploader(
        "Drag and drop files here",
        type=[e.lstrip(".") for e in ALLOWED_EXT],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state['uploader_version']}",
        help=f"Limite {MAX_UPLOAD_MB} Mo par fichier.",
    )

    if st.button("Enregistrer la mise à jour", type="primary"):
        # 1) Insert dans project_updates
        ok_update = insert_update(
            sb,
            project_id=project_id,
            progress_t=float(st.session_state["form_progress_travaux"] or 0),
            progress_p=float(st.session_state["form_progress_paiements"] or 0),
            commentaires=st.session_state["form_commentaires"] or "",
            pv_date=st.session_state["form_date_pv"],
        )
        # 2) Upload fichiers s’il y en a
        files_bytes = _bytes_from_uploader(uploaded)
        uploaded_count = 0
        warns: List[str] = []
        if files_bytes:
            uploaded_count, warns = upload_files(sb, project_id, files_bytes)

        if ok_update:
            st.success(f"Mise à jour enregistrée. Fichiers déposés : {uploaded_count}")
        if warns:
            st.warning("Quelques fichiers n’ont pas été pris en compte :\n- " + "\n- ".join(warns))

        # Reset des champs après enregistrement (garde le projet)
        _reset_form_state(reset_project=False)
        st.rerun()

    # ── Historique des PV (nouveau flux Storage)
    st.markdown("### Pièces jointes — PV de chantier")
    pv_list = list_signed_pv(sb, project_id)
    if not pv_list:
        st.info("Aucun PV pour ce projet.")
    else:
        for item in pv_list:
            nom = item["file_name"]
            url = item["url"]
            up_at = item.get("uploaded_at", "")
            st.markdown(f"- **[{nom}]({url})** — _ajouté le {up_at}_", unsafe_allow_html=True)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    sb = get_supabase()

    # Bandeau santé réseau
    host_ip = dns_ping_from_url(sb.rest_url if hasattr(sb, "rest_url") else os.getenv("SUPABASE_URL", ""))
    st.success(
        f"Connexion réseau Supabase OK (status attendu: 401) — DNS {os.getenv('SUPABASE_URL','').replace('https://','').split('/')[0]} "
        + (f"→ {host_ip}" if host_ip else "")
    )

    # Une seule colonne (pas de panneau latéral encombrant)
    # Auth (en haut) + app dessous
    auth_panel(sb)

    # Si pas connecté, on n’affiche pas la suite
    if not st.session_state["user"]:
        return

    st.title(APP_TITLE)

    # Sélection du projet
    project_id = seleccionar_projet(sb)
    if not project_id:
        return

    # Formulaire & historique
    form_panel(sb, project_id)


if __name__ == "__main__":
    main()
