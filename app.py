# ---------------- app.py (Partie 1/3) ----------------
import os, socket, traceback, uuid
from datetime import date, datetime
from typing import List, Dict, Any, Optional, Tuple

import streamlit as st
from supabase import create_client, Client

# ---------- Etat global (doit être en tout début) ----------
DEFAULT_STATE = {
    "user": None,                       # dict util. supabase
    "auth_panel_visible": True,         # afficher panneau login
    "selected_project_id": None,        # projet sélectionné
    "uploader_version": 0,              # reset file_uploader
    "form_progress_travaux": None,      # float|None
    "form_progress_paiements": None,    # float|None
    "form_date_pv": None,               # date|None
    "form_commentaires": "",            # str
}
for k, v in DEFAULT_STATE.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ---------- Config page ----------
st.set_page_config(
    page_title="Suivi d’avancement — Saisie hebdomadaire",
    page_icon="📊",
    layout="wide",
)

# ---------- Constantes ----------
BUCKET_PV = "pv-chantier"
MAX_UPLOAD_MB = 25
ALLOWED_EXT = {".pdf", ".docx", ".doc"}

# ---------- Utilitaires ----------
def banner(msg: str, kind: str = "info"):
    (st.info if kind=="info" else st.success if kind=="success" else st.warning if kind=="warning" else st.error)(msg)

def pct_or_none(x) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return None

def _coerce_to_date(v) -> Optional[date]:
    """Transforme str/datetime/date/None -> date|None (pour st.date_input)."""
    if v is None or v == "":
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except Exception:
            return None
    return None

# ---------- Supabase ----------
@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL") or st.secrets.get("SUPABASE_URL")
    key = os.getenv("SUPABASE_ANON_KEY") or st.secrets.get("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL et/ou SUPABASE_ANON_KEY manquent.")
    return create_client(url, key)

sb: Client = get_supabase()

def whoami(sb: Client) -> Optional[Dict[str, Any]]:
    try:
        info = sb.auth.get_user()
        return info.user.model_dump() if info and info.user else None
    except Exception:
        return None

def list_projects(sb: Client) -> List[Dict[str, Any]]:
    res = sb.table("projects").select("id,name").order("name").execute()
    return res.data or []

def insert_project_update(
    sb: Client,
    *,
    project_id: str,
    progress_travaux: Optional[float],
    progress_paiements: Optional[float],
    pv_date: Optional[date],
    commentaires: str,
) -> Tuple[bool, str]:
    try:
        payload = {
            "project_id": project_id,
            "updated_by": st.session_state["user"]["id"],
            "progress_travaux": progress_travaux,
            "progress_paiements": progress_paiements,
            "pv_chantier": pv_date.isoformat() if pv_date else None,
            "commentaires": commentaires or "",
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        res = sb.table("project_updates").insert(payload).execute()
        if res.data:
            return True, "Mise à jour enregistrée."
        return False, "Insertion vide."
    except Exception as e:
        return False, f"Erreur enregistrement: {e}"

def upload_files(
    sb: Client,
    project_id: str,
    files: List[Tuple[str, bytes]],
) -> Tuple[int, List[str]]:
    """files: [(filename, bytes)], retourne (nb_ok, warnings)"""
    ok = 0
    warns: List[str] = []
    today_str = datetime.utcnow().strftime("%Y%m%d")
    base_path = f"{project_id}/{today_str}"
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
                file=content,                      # BYTES (pas BytesIO)
                file_options={"contentType": "application/octet-stream"},
            )
            ok += 1
        except Exception as e:
            warns.append(f"{fname}: {e}")
    return ok, warns

def list_signed_pv(sb: Client, project_id: str, expires_sec: int = 3600) -> List[Dict[str, Any]]:
    """Liste fichiers Storage pour le projet, avec URL signées."""
    try:
        prefix = f"{project_id}/"
        items = sb.storage.from_(BUCKET_PV).list(path=prefix, search="")
        if not items:
            return []
        paths = [f"{prefix}{it['name']}" for it in items]
        signed = sb.storage.from_(BUCKET_PV).create_signed_urls(paths, expires_sec)
        signed_idx = {p["path"]: p["signedURL"] for p in signed}
        out = []
        for it in items:
            full = f"{prefix}{it['name']}"
            out.append({
                "file_name": it["name"],
                "url": signed_idx.get(full),
                "uploaded_at": it.get("created_at"),
            })
        out.sort(key=lambda x: x.get("uploaded_at") or "", reverse=True)
        return out
    except Exception:
        return []
# ---------------- fin Partie 1/3 ----------------
# ---------------- app.py (Partie 2/3) ----------------

def auth_panel(sb: Client):
    """Colonne de gauche pour l’auth. Masquée une fois connecté."""
    user = st.session_state["user"]
    if user:
        st.write(f"Connecté : **{user.get('email', 'compte')}**")
        if st.button("Se déconnecter"):
            try:
                sb.auth.sign_out()
            except Exception:
                pass
            # Reset état
            for k, v in DEFAULT_STATE.items():
                st.session_state[k] = v
            st.rerun()
        st.divider()
        return

    st.subheader("Connexion")
    mode = st.radio("", ["Se connecter", "Créer un compte"], label_visibility="collapsed")
    email = st.text_input("Email", key="auth_email")
    pwd = st.text_input("Mot de passe", type="password", key="auth_pwd")
    if st.button("Connexion" if mode=="Se connecter" else "Créer mon compte", type="primary"):
        try:
            if mode == "Se connecter":
                sb.auth.sign_in_with_password({"email": email, "password": pwd})
            else:
                sb.auth.sign_up({"email": email, "password": pwd})
            st.session_state["user"] = whoami(sb)
            st.session_state["auth_panel_visible"] = False
            st.rerun()
        except Exception as e:
            banner(f"Echec d'authentification : {e}", "error")

def _clear_form_on_project_change():
    """Ne JAMAIS modifier une clé qui est la clé d’un widget déjà instancié !"""
    st.session_state["form_progress_travaux"] = None
    st.session_state["form_progress_paiements"] = None
    st.session_state["form_date_pv"] = None
    st.session_state["form_commentaires"] = ""
    st.session_state["uploader_version"] += 1   # force le reset de l'uploader

def form_panel(sb: Client, projects: List[Dict[str, Any]]):
    st.header("Suivi d’avancement — Saisie")

    if not st.session_state["user"]:
        banner("Connecte-toi pour saisir une mise à jour.", "info")
        return

    # -------- Sélecteur projet --------
    options = {p["name"]: p["id"] for p in projects}
    if not options:
        banner("Aucun projet disponible.", "warning")
        return
    names = list(options.keys())
    current_name = None
    if st.session_state["selected_project_id"]:
        for n, pid in options.items():
            if pid == st.session_state["selected_project_id"]:
                current_name = n
                break

    chosen_name = st.selectbox(
        "Projet", names,
        index=(names.index(current_name) if current_name in names else 0),
        key="project_select_box"
    )
    chosen_pid = options[chosen_name]
    if chosen_pid != st.session_state["selected_project_id"]:
        st.session_state["selected_project_id"] = chosen_pid
        _clear_form_on_project_change()

    st.subheader("Nouvelle mise à jour")

    # -------- Champs de saisie --------
    c1, c2 = st.columns(2)

    with c1:
        # clé widget ≠ clé métier -> pas de conflit
        prog_trav = st.number_input(
            "Progression travaux (%)", min_value=0.0, max_value=100.0, step=1.0,
            value=st.session_state["form_progress_travaux"] if st.session_state["form_progress_travaux"] is not None else 0.0,
            key="form_progress_travaux_widget",
        )
        st.session_state["form_progress_travaux"] = prog_trav

    with c2:
        prog_pay = st.number_input(
            "Progression paiements (%)", min_value=0.0, max_value=100.0, step=1.0,
            value=st.session_state["form_progress_paiements"] if st.session_state["form_progress_paiements"] is not None else 0.0,
            key="form_progress_paiements_widget",
        )
        st.session_state["form_progress_paiements"] = prog_pay

    # IMPORTANT : on n’utilise PAS la même clé pour le widget et pour l’état métier
    initial_date = _coerce_to_date(st.session_state.get("form_date_pv"))
    date_choice = st.date_input(
        "Date du PV de chantier (optionnel)",
        value=initial_date,
        key="form_date_pv_widget"   # <-- clé widget distincte
    )
    st.session_state["form_date_pv"] = _coerce_to_date(date_choice)

    commentaires_val = st.text_area(
        "Commentaires",
        value=st.session_state["form_commentaires"],
        key="form_commentaires_widget"
    )
    st.session_state["form_commentaires"] = commentaires_val or ""

    st.markdown("### Joindre le PV (PDF/DOCX/DOC)")
    uploaded = st.file_uploader(
        "Déposer un ou plusieurs fichiers",
        type=["pdf", "docx", "doc"],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state['uploader_version']}",
        label_visibility="collapsed",
    )

    # -------- Enregistrement --------
    if st.button("Enregistrer la mise à jour", type="primary"):
        ok, msg = insert_project_update(
            sb,
            project_id=st.session_state["selected_project_id"],
            progress_travaux=pct_or_none(st.session_state["form_progress_travaux"]),
            progress_paiements=pct_or_none(st.session_state["form_progress_paiements"]),
            pv_date=_coerce_to_date(st.session_state["form_date_pv"]),
            commentaires=st.session_state["form_commentaires"],
        )
        (banner if ok else banner)(msg, "success" if ok else "error")

        # Upload Storage
        files_bytes: List[Tuple[str, bytes]] = []
        if uploaded:
            for f in uploaded:
                try:
                    files_bytes.append((f.name, f.getvalue()))
                except Exception:
                    pass
        if files_bytes:
            nb, warns = upload_files(sb, st.session_state["selected_project_id"], files_bytes)
            if nb:
                banner(f"Fichiers déposés : {nb}", "success")
            if warns:
                banner("Quelques fichiers n’ont pas été pris en compte :\n- " + "\n- ".join(warns), "warning")

        # Reset formulaire après succès
        _clear_form_on_project_change()
        st.rerun()

    # -------- Liste PV --------
    st.subheader("Pièces jointes — PV de chantier")
    pv_list = list_signed_pv(sb, st.session_state["selected_project_id"], expires_sec=3600)
    if not pv_list:
        st.info("Aucun PV pour ce projet.")
    else:
        for item in pv_list:
            st.markdown(
                f"- **[{item['file_name']}]({item['url']})**"
                + (f"  \n  _Uploadé le : {item.get('uploaded_at','')}_"
                   if item.get('uploaded_at') else ""),
                unsafe_allow_html=True
            )
# ---------------- fin Partie 2/3 ----------------
# ---------------- app.py (Partie 3/3) ----------------

def dns_banner():
    """Petit test réseau/DNS vers Supabase (optionnel)."""
    try:
        url = (os.getenv("SUPABASE_URL") or st.secrets.get("SUPABASE_URL", "")).strip()
        host = url.split("://", 1)[-1].split("/", 1)[0]
        ip = socket.gethostbyname(host)
        st.caption(f"Connexion réseau Supabase OK (status attendu: 401) — DNS {host} → {ip}")
    except Exception as e:
        st.caption(f"DNS Supabase: {e}")

def main():
    dns_banner()
    # rafraîchir user actuel
    st.session_state["user"] = whoami(sb)

    # Layout : si connecté et panneau masqué -> 1 colonne large ; sinon 2 colonnes
    if st.session_state["user"] and not st.session_state["auth_panel_visible"]:
        # pas de colonne auth
        form_panel(sb, list_projects(sb))
    else:
        col_auth, col_app = st.columns([1, 2], gap="large")
        with col_auth:
            auth_panel(sb)
        with col_app:
            form_panel(sb, list_projects(sb))

if __name__ == "__main__":
    try:
        main()
    except Exception:
        st.error("Une erreur est survenue. Détails dans les logs.")
        st.exception(traceback.format_exc())
# ---------------- fin Partie 3/3 ----------------
