# ---- app.py (Partie 1/3) -----------------------------------------------------
import os, socket, traceback, platform, uuid
from datetime import date, datetime
from typing import List, Dict, Any, Optional, Tuple

import streamlit as st
from supabase import create_client, Client

# ----------------- Etat global (toujours en haut) -----------------
DEFAULT_STATE = {
    "user": None,                       # objet utilisateur supabase
    "auth_panel_visible": True,         # afficher le panneau de connexion
    "selected_project_id": None,        # id projet sélectionné
    "uploader_version": 0,              # permet de "vider" le file_uploader
    "form_progress_travaux": None,      # float|None
    "form_progress_paiements": None,    # float|None
    "form_date_pv": None,               # datetime.date|None
    "form_commentaires": "",            # str
}
for k, v in DEFAULT_STATE.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ----------------- Config page -----------------
st.set_page_config(
    page_title="Suivi d’avancement — Saisie hebdomadaire",
    page_icon="📊",
    layout="wide",
)

# ----------------- Constantes -----------------
BUCKET_PV = "pv-chantier"
MAX_UPLOAD_MB = 25
ALLOWED_EXT = {".pdf", ".docx", ".doc"}

# ----------------- Utils d'affichage -----------------
def banner(msg: str, kind: str = "info"):
    if kind == "info":
        st.info(msg)
    elif kind == "success":
        st.success(msg)
    elif kind == "warning":
        st.warning(msg)
    else:
        st.error(msg)

def pct_or_none(x) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return None

# ----------------- Supabase -----------------
@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL") or st.secrets.get("SUPABASE_URL")
    key = os.getenv("SUPABASE_ANON_KEY") or st.secrets.get("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL et/ou SUPABASE_ANON_KEY manquent.")
    return create_client(url, key)

def whoami(sb: Client) -> Optional[Dict[str, Any]]:
    try:
        user = sb.auth.get_user()
        return user.user if user else None
    except Exception:
        return None

def list_projects(sb: Client) -> List[Dict[str, Any]]:
    # On ne ramène que id, name, trié par name
    data = sb.table("projects").select("id,name").order("name").execute()
    return data.data or []

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
        # Nettoyage des None pour PostgREST
        payload = {k: v for k, v in payload.items() if v is not None}
        res = sb.table("project_updates").insert(payload).execute()
        if res.data:
            return True, "Mise à jour enregistrée."
        return False, "Insertion vide (aucune ligne)."
    except Exception as e:
        return False, f"Erreur enregistrement: {e}"

def upload_files(
    sb: Client,
    project_id: str,
    files: List[Tuple[str, bytes]],
) -> Tuple[int, List[str]]:
    """
    files: liste de tuples (filename, bytes_content)
    Retourne (nb_ok, warnings)
    """
    ok = 0
    warns: List[str] = []
    # Dossier virtuel par projet + date
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
                file=content,      # bytes
                file_options={"contentType": "application/octet-stream"},
            )
            ok += 1
        except Exception as e:
            warns.append(f"{fname}: {e}")
    return ok, warns

def list_signed_pv(sb: Client, project_id: str, expires_sec: int = 3600) -> List[Dict[str, Any]]:
    """Liste les fichiers du bucket pour le projet, avec URLs signées."""
    try:
        prefix = f"{project_id}/"
        items = sb.storage.from_(BUCKET_PV).list(path=prefix, search="")
        out = []
        if not items:
            return out
        paths = [f"{prefix}{it['name']}" for it in items]
        # Génère des URLs signées par batch
        signed = sb.storage.from_(BUCKET_PV).create_signed_urls(paths, expires_sec)
        idx = {p["path"]: p["signedURL"] for p in signed}
        for it in items:
            full = f"{prefix}{it['name']}"
            out.append({
                "file_name": it["name"],
                "url": idx.get(full),
                "uploaded_at": it.get("created_at"),
            })
        # Tri inverse (les plus récents d’abord si possible)
        out.sort(key=lambda x: x.get("uploaded_at") or "", reverse=True)
        return out
    except Exception:
        return []
# ---- app.py (Partie 2/3) -----------------------------------------------------

def auth_panel(sb: Client):
    """Colonne gauche pour l’auth. On masque une fois connecté."""
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
        return

    st.subheader("Connexion")
    mode_creer = st.radio("",
                          ["Se connecter", "Créer un compte"],
                          horizontal=False, index=0, label_visibility="collapsed")

    email = st.text_input("Email", key="auth_email")
    pwd = st.text_input("Mot de passe", type="password", key="auth_pwd")
    if st.button("Connexion" if mode_creer == "Se connecter" else "Créer mon compte", type="primary"):
        try:
            if mode_creer == "Se connecter":
                sb.auth.sign_in_with_password({"email": email, "password": pwd})
            else:
                sb.auth.sign_up({"email": email, "password": pwd})
            st.session_state["user"] = whoami(sb)
            st.session_state["auth_panel_visible"] = False
            st.rerun()
        except Exception as e:
            banner(f"Echec d'authentification : {e}", "error")


def _clear_form_state_for_project_change():
    st.session_state["form_progress_travaux"] = None
    st.session_state["form_progress_paiements"] = None
    st.session_state["form_date_pv"] = None
    st.session_state["form_commentaires"] = ""
    # Vider l’uploader en incrémentant une clé
    st.session_state["uploader_version"] += 1

def form_panel(sb: Client, projects: List[Dict[str, Any]]):
    st.header("Suivi d’avancement — Saisie")

    if not st.session_state["user"]:
        banner("Connecte-toi pour saisir une mise à jour.", "info")
        return

    # --------- Sélecteur projet ----------
    options = {p["name"]: p["id"] for p in projects}
    names = list(options.keys())
    if not names:
        banner("Aucun projet disponible.", "warning")
        return

    # Retrouver le label courant si possible
    current_label = None
    if st.session_state["selected_project_id"]:
        for k, v in options.items():
            if v == st.session_state["selected_project_id"]:
                current_label = k
                break

    new_label = st.selectbox("Projet", names,
                             index=(names.index(current_label) if current_label in names else 0),
                             key="project_select_box")

    new_project_id = options[new_label]
    if new_project_id != st.session_state["selected_project_id"]:
        st.session_state["selected_project_id"] = new_project_id
        _clear_form_state_for_project_change()

    st.subheader("Nouvelle mise à jour")

    # --------- Champs de saisie ----------
    c1, c2 = st.columns(2)

    with c1:
        st.session_state["form_progress_travaux"] = st.number_input(
            "Progression travaux (%)", min_value=0.0, max_value=100.0, step=1.0,
            value=st.session_state["form_progress_travaux"] if st.session_state["form_progress_travaux"] is not None else 0.0,
            key="form_progress_travaux_widget",
        )

    with c2:
        st.session_state["form_progress_paiements"] = st.number_input(
            "Progression paiements (%)", min_value=0.0, max_value=100.0, step=1.0,
            value=st.session_state["form_progress_paiements"] if st.session_state["form_progress_paiements"] is not None else 0.0,
            key="form_progress_paiements_widget",
        )

    # Date facultative — valeur doit être `date|None`
    def _safe_date(v):
        return v if isinstance(v, date) else None

    st.session_state["form_date_pv"] = st.date_input(
        "Date du PV de chantier (optionnel)",
        value=_safe_date(st.session_state["form_date_pv"]),
        key="form_date_pv",
    )

    st.session_state["form_commentaires"] = st.text_area(
        "Commentaires",
        value=st.session_state["form_commentaires"],
        placeholder="Observations, risques, points bloquants…",
        key="form_commentaires_widget",
    )

    st.write("### Joindre le PV (PDF/DOCX/DOC)")
    # Le "uploader_version" dans la clé permet de forcer le reset
    uploaded = st.file_uploader(
        "Déposer un ou plusieurs fichiers",
        type=["pdf", "docx", "doc"],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state['uploader_version']}",
        label_visibility="collapsed",
    )

    # --------- Bouton Enregistrer ----------
    if st.button("Enregistrer la mise à jour", type="primary"):
        # 1) Insertion en base
        ok, msg = insert_project_update(
            sb,
            project_id=st.session_state["selected_project_id"],
            progress_travaux=pct_or_none(st.session_state["form_progress_travaux"]),
            progress_paiements=pct_or_none(st.session_state["form_progress_paiements"]),
            pv_date=st.session_state["form_date_pv"],
            commentaires=st.session_state["form_commentaires"],
        )
        if not ok:
            banner(msg, "error")
        else:
            banner(msg, "success")

        # 2) Upload Storage
        files_bytes: List[Tuple[str, bytes]] = []
        if uploaded:
            for f in uploaded:
                try:
                    files_bytes.append((f.name, f.getvalue()))
                except Exception:
                    pass
        if files_bytes:
            nb, warns = upload_files(
                sb, st.session_state["selected_project_id"], files_bytes
            )
            if nb:
                banner(f"Fichiers déposés : {nb}", "success")
            if warns:
                banner("Quelques fichiers n’ont pas été pris en compte :\n- " + "\n- ".join(warns), "warning")

        # 3) Reset partiel des champs (les PV restent listés dynamiquement)
        _clear_form_state_for_project_change()
        st.rerun()

    # --------- Liste PV du projet ----------
    st.subheader("Pièces jointes — PV de chantier")
    pv_list = list_signed_pv(sb, st.session_state["selected_project_id"], expires_sec=3600)
    if not pv_list:
        st.info("Aucun PV pour ce projet.")
    else:
        for item in pv_list:
            st.markdown(
                f"- **[{item['file_name']}]({item['url']})**"
                + (f"  \n  _Uploadé le : {item['uploaded_at']}_"
                   if item.get('uploaded_at') else ""),
                unsafe_allow_html=True,
            )
# ---- app.py (Partie 3/3) -----------------------------------------------------

def network_check(url: str):
    # Affiche un bandeau de santé minimal (401 attendu sans apikey)
    import httpx
    try:
        r = httpx.get(url + "/auth/v1/health", timeout=5.0)
    except Exception:
        try:
            r = httpx.get(url + "/auth/v1/user", timeout=5.0)
        except Exception as e:
            banner(f"Connexion réseau Supabase KO : {e}", "error")
            return
    banner(f"Connexion réseau Supabase OK (status attendu: 401) — DNS {url.split('//')[-1]}", "success")

def main():
    sb = get_supabase()

    # Petit bandeau de santé
    try:
        network_check(sb.rest_url.rstrip("/rest/v1"))
    except Exception:
        pass

    # Rafraîchir user
    st.session_state["user"] = whoami(sb)

    # Layout : 2 colonnes seulement si panneau auth affiché
    if st.session_state["user"] and not st.session_state["auth_panel_visible"]:
        # Masquer le panneau de gauche si déjà connecté
        form_panel(sb, list_projects(sb))
    else:
        col_auth, col_form = st.columns([1, 2], gap="large")
        with col_auth:
            auth_panel(sb)
        with col_form:
            form_panel(sb, list_projects(sb))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        st.error("Une erreur est survenue.")
        st.exception(e)
