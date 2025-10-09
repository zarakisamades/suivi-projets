# app.py
from __future__ import annotations

import os
import socket
from datetime import date
from typing import Optional

import streamlit as st
from supabase import create_client, Client


# -----------------------------
# Config de page
# -----------------------------
st.set_page_config(page_title="Suivi d’avancement — Saisie hebdomadaire", page_icon="📈", layout="centered")
st.title("Suivi d’avancement — Saisie hebdomadaire")


# -----------------------------
# Accès Supabase (mis en cache)
# -----------------------------
@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_ANON_KEY", "")
    if not url or not key:
        raise RuntimeError("Variables d’environnement SUPABASE_URL / SUPABASE_ANON_KEY manquantes.")
    return create_client(url, key)


def get_user_id(sb: Client) -> Optional[str]:
    """Retourne l'UUID de l'utilisateur connecté (ou None)."""
    try:
        u = sb.auth.get_user()
        # Structure: object avec .user.id (supabase-py v2)
        return getattr(getattr(u, "user", None), "id", None)
    except Exception:
        return None


# -----------------------------
# Healthcheck rapide
# -----------------------------
def healthcheck(sb: Client):
    url = os.getenv("SUPABASE_URL", "")
    host = url.replace("https://", "").replace("http://", "").split("/")[0] if url else ""
    try:
        ip = socket.gethostbyname(host)
        st.success(f"Connexion réseau Supabase OK (status attendu: 401) — DNS {host} → {ip}")
    except Exception as e:
        st.error(f"DNS échec : {e}")

    # Import/initialisation
    try:
        _ = sb.auth  # accès simple
        st.success("Import et initialisation de Supabase OK")
    except Exception as e:
        st.error(f"Erreur initialisation Supabase : {e}")


# -----------------------------
# UI Auth
# -----------------------------
def auth_panel(sb: Client):
    """Colonne gauche : connexion / création de compte."""
    with st.sidebar:
        st.header("Connexion")
        mode = st.radio("",
                        options=("Se connecter", "Créer un compte"),
                        index=0,
                        label_visibility="collapsed")

        email = st.text_input("Email", value="", placeholder="email@domaine.com")
        pwd = st.text_input("Mot de passe", value="", type="password")

        if mode == "Se connecter":
            if st.button("Connexion", use_container_width=True):
                try:
                    sb.auth.sign_in_with_password({"email": email, "password": pwd})
                    st.session_state["user_id"] = get_user_id(sb)
                    if st.session_state["user_id"]:
                        st.toast("Connecté ✅", icon="✅")
                        st.rerun()
                    else:
                        st.error("Échec de connexion : vérifie ton email (confirmé ?) et ton mot de passe.")
                except Exception as e:
                    st.error(f"Échec de connexion : {getattr(e, 'message', str(e))}")

        else:  # Créer un compte
            if st.button("Créer mon compte", use_container_width=True):
                try:
                    sb.auth.sign_up({"email": email, "password": pwd})
                    st.success("Compte créé ! Vérifie ton email et clique sur le lien de confirmation, puis reviens te connecter.")
                except Exception as e:
                    st.error(f"Échec de création : {getattr(e, 'message', str(e))}")

        # Zone compte
        st.divider()
        uid = st.session_state.get("user_id")
        if uid:
            st.caption(f"Connecté : {sb.auth.get_user().user.email}")
            if st.button("Se déconnecter", use_container_width=True):
                try:
                    sb.auth.sign_out()
                finally:
                    st.session_state.pop("user_id", None)
                    st.rerun()


# -----------------------------
# Contenu principal connecté
# -----------------------------
def app_connected(sb: Client, user_id: str):
    st.subheader("Nouvelle mise à jour")

    # Charger la liste des projets
    try:
        proj_res = sb.table("projects").select("id,name").order("name").execute()
        projects = proj_res.data or []
    except Exception as e:
        st.error(f"Erreur lecture projets : {e}")
        projects = []

    if not projects:
        st.info("Aucun projet trouvé dans la table **projects**.")
        return

    names = [p["name"] for p in projects]
    selected = st.selectbox("Projet", names)
    project_id = next((p["id"] for p in projects if p["name"] == selected), None)

    col1, col2 = st.columns(2)
    with col1:
        progress_travaux = st.number_input("Progression travaux (%)", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
    with col2:
        progress_paiements = st.number_input("Progression paiements (%)", min_value=0.0, max_value=100.0, value=0.0, step=1.0)

    pv_date = st.date_input("PV chantier (date)", value=date.today())
    commentaires = st.text_area("Commentaires (facultatif)", placeholder="Observations, risques, actions…")

    if st.button("Enregistrer la mise à jour", type="primary"):
        try:
            payload = {
                "project_id": project_id,
                "updated_by": user_id,              # RLS: with check auth.uid() = updated_by
                "progress_travaux": progress_travaux,
                "progress_paiements": progress_paiements,
                "pv_chantier": pv_date.isoformat(),
                "commentaires": commentaires or None,
            }
            sb.table("project_updates").insert(payload).execute()
            st.success("Mise à jour enregistrée ✅")
        except Exception as e:
            st.error(f"Échec d’insertion : {getattr(e, 'message', str(e))}")

    st.divider()
    st.subheader("Mes dernières saisies")
    try:
        upd = (
            sb.table("project_updates")
            .select("created_at, progress_travaux, progress_paiements, commentaires")
            .eq("updated_by", user_id)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        rows = upd.data or []
        if not rows:
            st.caption("Aucune saisie trouvée.")
        else:
            for r in rows:
                st.write(
                    f"- **{r['created_at']}** — travaux {r['progress_travaux']}%, paiements {r['progress_paiements']}%  \n"
                    f"  {r.get('commentaires') or ''}"
                )
    except Exception as e:
        st.error(f"Erreur lecture mises à jour : {e}")


# -----------------------------
# Main
# -----------------------------
def main():
    try:
        sb = get_supabase()
    except Exception as e:
        st.error(str(e))
        return

    # Bandeaux de contrôle
    healthcheck(sb)

    # Colonne auth (sidebar)
    auth_panel(sb)

    # Corps principal
    user_id = st.session_state.get("user_id") or get_user_id(sb)
    if user_id and user_id != st.session_state.get("user_id"):
        st.session_state["user_id"] = user_id  # synchronise

    st.write("")  # espacement
    if not user_id:
        st.info("Connecte-toi pour saisir une mise à jour.")
        return

    # Afficher l’app connectée
    app_connected(sb, user_id)


if __name__ == "__main__":
    main()
