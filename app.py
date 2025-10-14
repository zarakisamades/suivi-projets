# app.py

import os
import socket
import platform
from datetime import date, datetime
from typing import Optional, List, Dict

import streamlit as st
import pandas as pd
from supabase import create_client, Client

# ──────────────────────────────────────────────────────────────────────────────
# Configuration page
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Suivi d’avancement — Saisie hebdomadaire",
    page_icon="📈",
    layout="wide",
)

# ──────────────────────────────────────────────────────────────────────────────
# Constantes de l'appli
# ──────────────────────────────────────────────────────────────────────────────
BUCKET_PV = "pv-chantier"
MAX_UPLOAD_MB = 200
ALLOWED_EXT = {".pdf", ".docx", ".doc"}
SIGNED_URL_TTL = 60 * 60  # 1h

# ──────────────────────────────────────────────────────────────────────────────
# Session state (défauts)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_STATE = {
    "user": None,                     # objet user GoTrue
    "selected_project_id": None,      # uuid projet sélectionné
    "form_progress_travaux": 0.0,     # champs du formulaire
    "form_progress_paiements": 0.0,
    "form_commentaires": "",
    "form_date_pv": None,             # None | date
}

for k, v in DEFAULT_STATE.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ──────────────────────────────────────────────────────────────────────────────
# Supabase
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "Variables d’environnement SUPABASE_URL / SUPABASE_ANON_KEY manquantes."
        )
    return create_client(url, key)

def check_dns(url: str) -> str:
    try:
        host = url.split("://", 1)[-1].split("/", 1)[0]
        ip = socket.gethostbyname(host)
        return f"Connexion réseau Supabase OK (status attendu: 401) — DNS {host} → {ip}"
    except Exception as e:
        return f"DNS KO: {e}"

sb: Client = get_supabase()
st.success(check_dns(sb.rest_url))

# ──────────────────────────────────────────────────────────────────────────────
# Data helpers
# ──────────────────────────────────────────────────────────────────────────────
def list_projects(sb: Client) -> List[Dict]:
    """Retourne [{id,name}] triés par name."""
    try:
        data = sb.table("projects").select("id,name").order("name").execute()
        return data.data or []
    except Exception as e:
        st.error(f"Erreur lecture projets : {e}")
        return []

def insert_project_update(
    sb: Client,
    project_id: str,
    progress_travaux: float,
    progress_paiements: float,
    commentaires: str,
    pv_date: Optional[date],
) -> Optional[str]:
    """Insère une ligne dans project_updates. Retourne id ou None."""
    payload = {
        "project_id": project_id,
        "progress_travaux": progress_travaux,
        "progress_paiements": progress_paiements,
        "commentaires": commentaires or "",
    }
    if pv_date:
        payload["pv_chantier"] = pv_date.isoformat()

    try:
        res = sb.table("project_updates").insert(payload).select("id").single().execute()
        return (res.data or {}).get("id")
    except Exception as e:
        st.error(f"Erreur enregistrement mise à jour : {e}")
        return None

def _safe_filename(name: str) -> str:
    return (
        name.replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
        .replace("?", "_")
        .replace("#", "_")
        .replace("%", "_")
    )

def upload_pv_files(sb: Client, project_id: str, files: List[st.runtime.uploaded_file_manager.UploadedFile]) -> int:
    """Upload des fichiers dans pv-chantier/<project_id>/<YYYYMMDD>/..."""
    if not files:
        return 0

    today_folder = datetime.utcnow().strftime("%Y%m%d")
    ok_count = 0

    store = sb.storage.from_(BUCKET_PV)

    for f in files:
        name = _safe_filename(f.name or "pv.pdf")
        # extension check
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            st.warning(f"{name} ignoré : extension non autorisée.")
            continue

        # taille (UploadedFile -> len = bytes)
        size_mb = (f.size or 0) / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            st.warning(f"{name} ignoré : {size_mb:.1f} Mo > {MAX_UPLOAD_MB} Mo.")
            continue

        # path final
        uid = sb.auth.get_user().user.id if sb.auth.get_user() else "nouser"
        path = f"{project_id}/{today_folder}/{uid}_{name}"

        # upload (bytes)
        try:
            store.upload(path=path, file=f.getvalue(), file_options={"upsert": True, "content_type": f.type})
            ok_count += 1
        except Exception as e:
            st.warning(f"Upload échoué pour {name} : {e}")

    return ok_count

def list_signed_pv(sb: Client, project_id: str, expires_sec: int = SIGNED_URL_TTL) -> List[Dict]:
    """
    Parcourt pv-chantier/<project_id>/ et génère des URLs signées.
    Retourne [{file_name, url, uploaded_at}]
    """
    store = sb.storage.from_(BUCKET_PV)
    out: List[Dict] = []

    try:
        # 1er niveau = jours (YYYYMMDD)
        days = store.list(path=project_id)
    except Exception as e:
        st.error(f"Erreur lecture Storage : {e}")
        return out

    for d in days:
        if d.get("id"):  # fichiers racine potentiels (on ignore)
            continue
        folder = d.get("name")
        if not folder:
            continue

        try:
            files = store.list(path=f"{project_id}/{folder}")
        except Exception:
            continue

        for item in files:
            if not item.get("name"):
                continue
            obj_path = f"{project_id}/{folder}/{item['name']}"
            try:
                signed = store.create_signed_url(obj_path, expires_sec)
                url = signed.get("signedURL") or signed.get("signed_url") or signed.get("url") or ""
            except Exception:
                url = ""
            out.append(
                {
                    "file_name": item["name"],
                    "url": url,
                    "uploaded_at": item.get("updated_at") or item.get("created_at") or datetime.utcnow().isoformat(),
                }
            )

    # tri décroissant sur uploaded_at
    def _dt(x):
        try:
            return pd.to_datetime(x)
        except Exception:
            return pd.Timestamp.min

    out.sort(key=lambda r: _dt(r["uploaded_at"]), reverse=True)
    return out

# ──────────────────────────────────────────────────────────────────────────────
# UI : Rendu lisible de l’historique
# ──────────────────────────────────────────────────────────────────────────────
def _shorten_name(name: str, max_len: int = 60) -> str:
    if not isinstance(name, str):
        return name
    if len(name) <= max_len:
        return name
    keep = (max_len - 1) // 2
    return f"{name[:keep]}…{name[-keep:]}"

def render_pv_history(pv_list: List[Dict]):
    st.subheader("Pièces jointes — PV de chantier")
    if not pv_list:
        st.info("Aucun PV pour ce projet.")
        return

    rows = []
    now = datetime.utcnow()
    for it in pv_list:
        ts = it.get("uploaded_at")
        try:
            dtv = pd.to_datetime(ts)
        except Exception:
            dtv = pd.NaT
        rows.append(
            {
                "Date": (dtv.date().isoformat() if pd.notna(dtv) else ""),
                "Fichier": _shorten_name(it.get("file_name", "")),
                "Ouvrir": it.get("url", ""),
                "Ajouté le": (dtv.tz_localize(None).isoformat() if pd.notna(dtv) else ""),
                "Lien valide ~jusqu’à": (now + pd.Timedelta(seconds=SIGNED_URL_TTL)).isoformat(),
            }
        )

    df = pd.DataFrame(rows).sort_values(["Date", "Ajouté le"], ascending=[False, False])

    for day, group in df.groupby("Date", sort=False):
        with st.expander(day or "Date inconnue", expanded=True):
            st.dataframe(
                group[["Fichier", "Ouvrir", "Ajouté le", "Lien valide ~jusqu’à"]].reset_index(drop=True),
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Fichier": st.column_config.TextColumn("Fichier", width="large"),
                    "Ouvrir": st.column_config.LinkColumn("Lien", display_text="Ouvrir"),
                    "Ajouté le": st.column_config.TextColumn("Ajouté le"),
                    "Lien valide ~jusqu’à": st.column_config.TextColumn("Lien valide ~jusqu’à"),
                },
            )

# ──────────────────────────────────────────────────────────────────────────────
# Auth très simple (email/password) – optionnel
# ──────────────────────────────────────────────────────────────────────────────
def auth_panel():
    st.markdown("### Connexion")
    mode = st.radio(" ", ["Se connecter", "Créer un compte"], horizontal=False, label_visibility="collapsed")

    email = st.text_input("Email", value="")
    pwd = st.text_input("Mot de passe", type="password", value="")

    colA, colB = st.columns([1, 1])
    with colA:
        if st.button("Connexion", use_container_width=True, disabled=not email or not pwd):
            try:
                sb.auth.sign_in_with_password({"email": email, "password": pwd})
                user = sb.auth.get_user().user
                st.session_state.user = user
                st.success(f"Connecté : {user.email}")
                st.rerun()
            except Exception as e:
                st.error(f"Échec connexion : {e}")
    with colB:
        if st.button("Se déconnecter", use_container_width=True):
            try:
                sb.auth.sign_out()
            except Exception:
                pass
            st.session_state.user = None
            st.session_state.selected_project_id = None
            st.rerun()

    if mode == "Créer un compte":
        st.write("— ou —")
        new_email = st.text_input("Email (inscription)", value="", key="signup_email")
        new_pwd = st.text_input("Mot de passe (min 6)", type="password", value="", key="signup_pwd")
        if st.button("Créer mon compte", use_container_width=True, disabled=not new_email or not new_pwd):
            try:
                sb.auth.sign_up({"email": new_email, "password": new_pwd})
                st.success("Compte créé. Vérifiez votre email puis connectez-vous.")
            except Exception as e:
                st.error(f"Échec de création du compte : {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Formulaire principal
# ──────────────────────────────────────────────────────────────────────────────
def form_panel(sb: Client, projects: List[Dict]):
    st.markdown("## Suivi d’avancement — Saisie")

    # Sélecteur de projet
    options = {p["name"]: p["id"] for p in projects}
    if not options:
        st.info("Aucun projet trouvé dans la table *projects*.")
        return

    # valeur actuelle (si connue) pour le selectbox
    current_id = st.session_state.get("selected_project_id")
    initial_name = None
    if current_id:
        for p in projects:
            if p["id"] == current_id:
                initial_name = p["name"]
                break

    sel_name = st.selectbox(
        "Projet",
        list(options.keys()),
        index=(list(options.keys()).index(initial_name) if initial_name in options else 0),
        key="ui_project_select",
    )
    project_id = options[sel_name]
    # met à jour l’état si change
    if project_id != st.session_state.get("selected_project_id"):
        st.session_state.selected_project_id = project_id
        # reset soft de la saisie lors du changement de projet
        st.session_state.form_progress_travaux = 0.0
        st.session_state.form_progress_paiements = 0.0
        st.session_state.form_commentaires = ""
        st.session_state.form_date_pv = None

    # Formulaire dans un "form" pour soumettre d’un coup
    with st.form("form_update", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            prg_t = st.number_input(
                "Progression travaux (%)",
                min_value=0.0,
                max_value=100.0,
                step=0.5,
                value=float(st.session_state.form_progress_travaux or 0.0),
                key="widget_prg_t",
            )
        with col2:
            prg_p = st.number_input(
                "Progression paiements (%)",
                min_value=0.0,
                max_value=100.0,
                step=0.5,
                value=float(st.session_state.form_progress_paiements or 0.0),
                key="widget_prg_p",
            )

        # IMPORTANT : ne pas écrire dans session_state après instanciation
        pv_date: Optional[date] = st.date_input(
            "Date du PV de chantier (optionnel)",
            value=(st.session_state.form_date_pv if isinstance(st.session_state.form_date_pv, date) else None),
            key="widget_pv_date",
            format="YYYY/MM/DD",
        )

        commentaires = st.text_area(
            "Commentaires",
            value=st.session_state.form_commentaires or "",
            height=140,
            key="widget_comments",
        )

        st.markdown("### Joindre le PV (PDF/DOCX/DOC)")
        files = st.file_uploader(
            " ",
            accept_multiple_files=True,
            type=["pdf", "docx", "doc"],
            help=f"Limite {MAX_UPLOAD_MB}MB par fichier • PDF, DOCX, DOC",
            label_visibility="collapsed",
            key="widget_pv_files",
        )

        submit = st.form_submit_button("Enregistrer la mise à jour", use_container_width=True)

    if submit:
        # 1) insert DB
        rec_id = insert_project_update(
            sb=sb,
            project_id=project_id,
            progress_travaux=float(prg_t),
            progress_paiements=float(prg_p),
            commentaires=commentaires,
            pv_date=pv_date,
        )
        if rec_id:
            # 2) upload fichiers
            uploaded = upload_pv_files(sb, project_id, files or [])
            if uploaded:
                st.success(f"Mise à jour enregistrée. Fichiers déposés : {uploaded}")
            else:
                st.success("Mise à jour enregistrée.")

            # 3) mettre à jour l’état (pour prochaine saisie)
            st.session_state.form_progress_travaux = prg_t
            st.session_state.form_progress_paiements = prg_p
            st.session_state.form_commentaires = commentaires
            st.session_state.form_date_pv = pv_date

            st.experimental_rerun()
        else:
            st.error("Échec d’enregistrement (voir message ci-dessus).")

    # Historique PV du projet (signé 1h)
    pv_list = list_signed_pv(sb, project_id, SIGNED_URL_TTL)
    render_pv_history(pv_list)

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def main():
    user = None
    try:
        u = sb.auth.get_user()
        user = u.user if u else None
    except Exception:
        user = None

    st.markdown(f"Connecté : **{(user.email if user else 'non connecté')}**")

    if not user:
        # Deux colonnes pour garder une structure claire
        col_auth, _ = st.columns([1, 2], vertical_alignment="top")
        with col_auth:
            auth_panel()
        return

    # Utilisateur connecté → formulaire
    projects = list_projects(sb)
    form_panel(sb, projects)

if __name__ == "__main__":
    main()
