# app.py
import os
from datetime import date
from typing import Dict, Any, List, Optional

import streamlit as st
from supabase import create_client, Client
import httpx

PAGE_TITLE = "Suivi d’avancement — Saisie hebdomadaire"


# ------------- Connexion Supabase (mise en cache) -------------
@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Secrets manquants: SUPABASE_URL / SUPABASE_ANON_KEY")
    return create_client(url, key)


def get_user(sb: Client):
    try:
        res = sb.auth.get_user()
        return res.user
    except Exception:
        return None


# ------------- Utilitaires DB -------------
def fetch_projects(sb: Client) -> List[Dict[str, Any]]:
    """
    Retourne [{id, label}] où label essaye de deviner la bonne colonne (name/nom/titre)
    """
    cols_try = ["id,name", "id,nom", "id,titre", "id,label"]
    for cols in cols_try:
        try:
            data = sb.table("projects").select(cols).order("id").execute().data
            if not data:
                continue
            # Normalise
            out = []
            for r in data:
                label = r.get("name") or r.get("nom") or r.get("titre") or r.get("label") or r["id"]
                out.append({"id": r["id"], "label": label})
            return out
        except Exception:
            continue
    return []


def insert_update(
    sb: Client,
    payload: Dict[str, Any],
) -> None:
    sb.table("project_updates").insert(payload).execute()


def fetch_last_updates(sb: Client, user_id: Optional[str]) -> List[Dict[str, Any]]:
    """
    Dernières mises à jour (côté utilisateur connecté)
    Essaie plusieurs colonnes triables (pv_chantier, created_at, inserted_at).
    """
    base = sb.table("project_updates").select("*")
    if user_id:
        base = base.eq("updated_by", user_id)
    for col in ["pv_chantier", "created_at", "inserted_at", "id"]:
        try:
            data = base.order(col, desc=True).limit(50).execute().data
            return data or []
        except Exception:
            continue
    # fallback sans tri
    try:
        return base.limit(50).execute().data or []
    except Exception:
        return []


# ------------- UI -------------
def render_auth(sb: Client):
    st.sidebar.subheader("Connexion")

    mode = st.sidebar.radio(
        " ",
        options=["Se connecter", "Créer un compte"],
        index=0,
        label_visibility="collapsed",
    )

    if "auth_msg" not in st.session_state:
        st.session_state["auth_msg"] = ""

    if mode == "Créer un compte":
        with st.sidebar.form("signup"):
            email = st.text_input("Email", key="signup_email")
            pwd = st.text_input("Mot de passe (min 6)", type="password", key="signup_pwd")
            ok = st.form_submit_button("Créer mon compte")
        if ok:
            if not email or not pwd:
                st.session_state.auth_msg = "Email / mot de passe requis."
            else:
                try:
                    sb.auth.sign_up({"email": email, "password": pwd})
                    st.success("Compte créé avec succès ! Connecte-toi maintenant.")
                except Exception as e:
                    st.session_state.auth_msg = f"Échec de création : {e}"
    else:
        with st.sidebar.form("signin"):
            email = st.text_input("Email", key="signin_email")
            pwd = st.text_input("Mot de passe", type="password", key="signin_pwd")
            ok = st.form_submit_button("Connexion")
        if ok:
            try:
                sb.auth.sign_in_with_password({"email": email, "password": pwd})
                st.session_state.auth_msg = ""
                st.experimental_rerun()
            except Exception as e:
                st.session_state.auth_msg = f"Échec de connexion : {e}"

    if st.session_state.auth_msg:
        st.sidebar.error(st.session_state.auth_msg)

    user = get_user(sb)
    if user:
        with st.sidebar.expander("Compte"):
            st.write(f"Connecté : **{user.email}**")
            if st.button("Se déconnecter"):
                sb.auth.sign_out()
                st.experimental_rerun()


def render_header(sb: Client):
    st.set_page_config(page_title=PAGE_TITLE, page_icon="📈", layout="centered")
    st.title(PAGE_TITLE)

    # Bannières de santé (réseau + client importé)
    ok_network = False
    try:
        # Un GET sur /rest/v1 (retourne 401 si reachable sans apikey → c’est ce qu’on veut)
        httpx.get(get_supabase().rest_url, timeout=5.0)
        ok_network = True
    except Exception:
        pass

    if ok_network:
        st.success("Connexion réseau Supabase OK (status attendu: 401)")
    else:
        st.error("Erreur réseau vers Supabase")

    # Client OK
    st.success("Import et initialisation de Supabase OK")


def render_form(sb: Client, user_id: str):
    st.subheader("Nouvelle mise à jour")
    projects = fetch_projects(sb)
    if not projects:
        st.info("Aucun projet trouvé dans la table `projects`.")
        return

    labels = [p["label"] for p in projects]
    labels_idx = 0
    col1, col2 = st.columns(2)
    with st.form("update_form", clear_on_submit=True):
        pid = st.selectbox("Projet", labels, index=labels_idx)
        selected = next((p for p in projects if p["label"] == pid), projects[0])

        with col1:
            prog_trav = st.number_input("Progress Travaux (%)", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
            pv_date = st.date_input("PV chantier (date)", value=date.today())
        with col2:
            prog_pay = st.number_input("Progress Paiements (%)", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
            commentaires = st.text_input("Commentaires", value="")

        extras = st.text_area("Extras (JSON facultatif)", value="", help='Ex: {"risques":"pluie"}')
        submitted = st.form_submit_button("Enregistrer")

    if submitted:
        extras_obj: Optional[Dict[str, Any]] = None
        if extras.strip():
            try:
                import json
                extras_obj = json.loads(extras)
            except Exception:
                st.warning("Extras ignoré (JSON invalide).")

        payload = {
            "project_id": selected["id"],
            "updated_by": user_id,
            "progress_travaux": prog_trav,
            "progress_paiements": prog_pay,
            "pv_chantier": pv_date.isoformat(),
            "commentaires": commentaires or None,
        }
        if extras_obj is not None:
            payload["extras"] = extras_obj

        try:
            insert_update(sb, payload)
            st.success("Mise à jour enregistrée ✅")
        except Exception as e:
            st.error(f"Échec d’enregistrement : {e}")


def render_table(sb: Client, user_id: str):
    st.subheader("Mes dernières saisies")
    data = fetch_last_updates(sb, user_id)
    if not data:
        st.info("Aucune saisie trouvée.")
        return

    # Normalisation d’affichage
    import pandas as pd

    df = pd.DataFrame(data)
    # Quelques colonnes utiles si présentes
    cols_pref = [c for c in [
        "pv_chantier", "project_id", "progress_travaux", "progress_paiements",
        "commentaires", "updated_by", "created_at", "inserted_at"
    ] if c in df.columns]
    if cols_pref:
        df = df[cols_pref]
    st.dataframe(df, use_container_width=True, hide_index=True)


def main():
    sb = get_supabase()
    render_header(sb)
    render_auth(sb)

    user = get_user(sb)

    st.markdown("---")
    if not user:
        st.info("Connecte-toi pour saisir une mise à jour.")
        return

    render_form(sb, user.id)
    st.markdown("---")
    render_table(sb, user.id)


if __name__ == "__main__":
    main()
