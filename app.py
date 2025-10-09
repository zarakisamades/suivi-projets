# app.py
from __future__ import annotations

import os
import socket
import re
import mimetypes
from datetime import date, datetime
from typing import Optional, Dict, Any, List

import streamlit as st
from supabase import create_client, Client


# =========================
# Config Streamlit
# =========================
st.set_page_config(page_title="Suivi d’avancement — Saisie hebdomadaire", page_icon="📈", layout="centered")
st.title("Suivi d’avancement — Saisie hebdomadaire")


# =========================
# Connexion Supabase (cache)
# =========================
@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    # Secrets Streamlit ou variables d'env (Share → Settings → Secrets)
    url = os.getenv("SUPABASE_URL", "").strip() or st.secrets.get("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_ANON_KEY", "").strip() or st.secrets.get("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Variables SUPABASE_URL / SUPABASE_ANON_KEY manquantes (Secrets).")
    return create_client(url, key)


def get_user_id(sb: Client) -> Optional[str]:
    try:
        u = sb.auth.get_user()
        return getattr(getattr(u, "user", None), "id", None)
    except Exception:
        return None


# =========================
# Healthcheck rapide
# =========================
def healthcheck(sb: Client):
    url = os.getenv("SUPABASE_URL", "") or st.secrets.get("SUPABASE_URL", "")
    host = url.replace("https://", "").replace("http://", "").split("/")[0] if url else ""
    try:
        ip = socket.gethostbyname(host)
        st.success(f"Connexion réseau Supabase OK — DNS {host} → {ip}")
    except Exception as e:
        st.error(f"DNS échec : {e}")

    try:
        _ = sb.auth
        st.success("Import et initialisation de Supabase OK")
    except Exception as e:
        st.error(f"Erreur initialisation Supabase : {e}")


# =========================
# Auth (sidebar)
# =========================
def auth_panel(sb: Client):
    with st.sidebar:
        st.header("Connexion")
        mode = st.radio("Choisir une action", options=("Se connecter", "Créer un compte"))

        email = st.text_input("Email", placeholder="email@domaine.com", key="auth_email")
        pwd = st.text_input("Mot de passe", type="password", key="auth_pwd")

        if mode == "Se connecter":
            if st.button("Connexion", use_container_width=True):
                try:
                    sb.auth.sign_in_with_password({"email": email, "password": pwd})
                    st.session_state["user_id"] = get_user_id(sb)
                    if st.session_state["user_id"]:
                        st.toast("Connecté ✅")
                        st.rerun()
                    else:
                        st.error("Connexion impossible (email confirmé ? mot de passe ?).")
                except Exception as e:
                    st.error(f"Échec de connexion : {getattr(e, 'message', str(e))}")
        else:
            if st.button("Créer mon compte", use_container_width=True):
                try:
                    sb.auth.sign_up({"email": email, "password": pwd})
                    st.success("Compte créé ! Confirme l’email reçu, puis connecte-toi.")
                except Exception as e:
                    st.error(f"Échec de création : {getattr(e, 'message', str(e))}")

        st.divider()
        uid = st.session_state.get("user_id")
        if uid:
            try:
                u = sb.auth.get_user().user
                st.caption(f"Connecté : {u.email}")
            except Exception:
                pass
            if st.button("Se déconnecter", use_container_width=True):
                try:
                    sb.auth.sign_out()
                finally:
                    st.session_state.pop("user_id", None)
                    st.rerun()


# =========================
# Data helpers
# =========================
def fetch_projects(sb: Client) -> List[Dict[str, Any]]:
    try:
        res = sb.table("projects").select("id,name").order("name").execute()
        return res.data or []
    except Exception:
        return []


def insert_update(sb: Client, payload: Dict[str, Any]) -> None:
    sb.table("project_updates").insert(payload).execute()


def fetch_last_updates(sb: Client, user_id: str) -> List[Dict[str, Any]]:
    base = sb.table("project_updates").select("*").eq("updated_by", user_id)
    for col in ["pv_chantier", "created_at", "id"]:
        try:
            return base.order(col, desc=True).limit(20).execute().data or []
        except Exception:
            continue
    return base.limit(20).execute().data or []


# =========================
# PV de chantier (Storage)
# =========================
BUCKET_PV = "pv-chantier"

def _safe_filename(name: str) -> str:
    base = re.sub(r"\s+", "_", name.strip())
    base = re.sub(r"[^A-Za-z0-9._-]", "", base)
    return base or f"pv_{int(datetime.utcnow().timestamp())}.pdf"

def upload_pv_file(sb: Client, project_id: str, file_obj) -> None:
    """
    Upload dans Storage (bucket privé pv-chantier) + insert metadata dans public.project_pv
    Pré-requis:
      - bucket 'pv-chantier' existe
      - table public.project_pv (file_path, file_name, mime_type, project_id, uploaded_at, uploaded_by)
      - policies Storage + table en place pour 'authenticated'
    """
    now = datetime.utcnow()
    ts = now.strftime("%Y-%m-%d_%H-%M-%S")
    fname = _safe_filename(file_obj.name)
    path = f"{project_id}/{now.year}/{ts}_{fname}"

    mime = getattr(file_obj, "type", None) or mimetypes.guess_type(fname)[0] or "application/octet-stream"

    up = sb.storage.from_(BUCKET_PV).upload(
        path=path,
        file=file_obj.getvalue(),
        file_options={"content-type": mime, "upsert": False},
    )
    if isinstance(up, dict) and up.get("error"):
        # certaines versions renvoient {"error": {...}} sur échec
        raise RuntimeError(f"Upload Storage: {up['error'].get('message', up['error'])}")

    sb.table("project_pv").insert({
        "project_id": project_id,
        "file_path": path,
        "file_name": file_obj.name,
        "mime_type": mime,
    }).execute()

def list_signed_pv(sb: Client, project_id: str, expires_sec: int = 3600) -> List[Dict[str, Any]]:
    sel = sb.table("project_pv")\
            .select("file_name, file_path, uploaded_at")\
            .eq("project_id", project_id)\
            .order("uploaded_at", desc=True)\
            .execute()
    rows = sel.data or []
    out = []
    for r in rows:
        signed = sb.storage.from_(BUCKET_PV).create_signed_url(r["file_path"], expires_sec)
        url = signed.get("signedURL") or signed.get("signed_url")
        out.append({"file_name": r["file_name"], "uploaded_at": r["uploaded_at"], "url": url})
    return out


# =========================
# Page connectée
# =========================
def page_nouvelle_mise_a_jour(sb: Client, user_id: str):
    st.subheader("Nouvelle mise à jour")

    # ---------- Sélecteur de projet ----------
    projects = fetch_projects(sb)
    if not projects:
        st.info("Aucun projet trouvé dans la table **projects**.")
        return
    names = [p["name"] for p in projects]
    selected_name = st.selectbox("Projet", names, index=0)
    project_id = next((p["id"] for p in projects if p["name"] == selected_name), None)
    if not project_id:
        st.error("Projet invalide.")
        return

    # ---------- Formulaire indicateurs ----------
    col1, col2 = st.columns(2)
    with col1:
        progress_travaux = st.number_input("Progression travaux (%)", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
        pv_date = st.date_input("PV chantier (date)", value=date.today())
    with col2:
        progress_paiements = st.number_input("Progression paiements (%)", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
        commentaires = st.text_area("Commentaires (facultatif)", placeholder="Observations, risques, actions…")

    if st.button("Enregistrer la mise à jour", type="primary"):
        try:
            payload = {
                "project_id": project_id,
                "updated_by": user_id,  # RLS: with check (auth.uid() = updated_by)
                "progress_travaux": progress_travaux,
                "progress_paiements": progress_paiements,
                "pv_chantier": pv_date.isoformat(),
                "commentaires": commentaires or None,
            }
            insert_update(sb, payload)
            st.success("Mise à jour enregistrée ✅")
        except Exception as e:
            st.error(f"Échec d’insertion : {getattr(e, 'message', str(e))}")

    st.divider()

    # ---------- PV de chantier : Upload + Historique ----------
    st.subheader("📎 PV de chantier (PDF / DOCX)")
    colu1, colu2 = st.columns([2, 1])

    with colu1:
        files = st.file_uploader(
            "Dépose un ou plusieurs fichiers",
            type=["pdf", "doc", "docx"],
            accept_multiple_files=True,
            key="pv_uploader"
        )
    with colu2:
        st.write("")  # espacement
        upload_clicked = st.button("Uploader les PV", use_container_width=True, disabled=not files)

    if upload_clicked and files:
        ok, ko = 0, 0
        with st.spinner("Upload en cours…"):
            for f in files:
                try:
                    upload_pv_file(sb, project_id, f)
                    ok += 1
                except Exception as e:
                    ko += 1
                    st.error(str(e))
        if ok:
            st.success(f"{ok} fichier(s) uploadé(s) ✔️")
        if ko:
            st.warning(f"{ko} fichier(s) en erreur ⚠️")
        st.rerun()

    st.markdown("#### PV déjà uploadés")
    # ========= HISTORIQUE COMPLET LEGACY (project_pv_log) =========
# A coller à la fin de page_nouvelle_mise_a_jour(sb, user_id), après la section PV existante.

def _pick_first(d, candidates, default=None):
    """Retourne la première clé existante dans d parmi 'candidates'."""
    for k in candidates:
        if k in d and d[k] is not None:
            return d[k]
    return default

def fetch_pv_log_rows(sb: Client, project_id: str, limit: int = 200):
    """
    Lit la table legacy project_pv_log (renommée depuis 'suivi des PV')
    et tente de normaliser les champs essentiels.
    """
    try:
        # On récupère "toutes colonnes" pour s'adapter aux noms
        q = sb.table("project_pv_log").select("*").eq("project_id", project_id).order("uploaded_at", desc=True)
        try:
            rows = q.limit(limit).execute().data or []
        except Exception:
            # Si la colonne uploaded_at n’existe pas, on tente created_at
            q = sb.table("project_pv_log").select("*").eq("project_id", project_id).order("created_at", desc=True)
            rows = q.limit(limit).execute().data or []
    except Exception as e:
        st.info("Aucun historique legacy (project_pv_log) détecté ou accès refusé.")
        return []

    norm = []
    for r in rows:
        # Détecte les noms des colonnes
        file_path = _pick_first(r, ["file_path", "storage_path", "path", "object_path"])
        file_name = _pick_first(r, ["file_name", "original_name", "name", "title"], default="PV_chantier.pdf")
        ts       = _pick_first(r, ["uploaded_at", "created_at", "date", "ts"])

        if not file_path:
            # Si on ne trouve aucun chemin, on ignore cette ligne (non cliquable)
            continue

        # URL signée temporaire
        try:
            signed = sb.storage.from_(BUCKET_PV).create_signed_url(file_path, 3600)
            url = signed.get("signedURL") or signed.get("signed_url")
        except Exception:
            url = None

        norm.append({
            "file_name": file_name,
            "uploaded_at": ts,
            "url": url,
            "file_path": file_path
        })
    return norm

with st.expander("🗂️ Historique complet des PV (source legacy `project_pv_log`)", expanded=False):
    legacy = fetch_pv_log_rows(sb, project_id, limit=200)
    if not legacy:
        st.caption("Aucun enregistrement legacy trouvé pour ce projet.")
    else:
        for item in legacy:
            if item["url"]:
                st.markdown(
                    f"- **[{item['file_name']}]({item['url']})**  \n"
                    f"  _Uploadé le : {item['uploaded_at']}_  \n"
                    f"  <span style='opacity:0.6'>({item['file_path']})</span>",
                    unsafe_allow_html=True
                )
            else:
                # Fallback si pas d’URL signée possible
                st.markdown(
                    f"- **{item['file_name']}**  \n"
                    f"  _Uploadé le : {item['uploaded_at']}_  \n"
                    f"  <span style='opacity:0.6'>Chemin : {item['file_path']}</span>",
                    unsafe_allow_html=True
                )

    pv_list = list_signed_pv(sb, project_id, expires_sec=3600)
    if not pv_list:
        st.info("Aucun PV trouvé pour ce projet.")
    else:
        for item in pv_list:
            st.markdown(
                f"- **[{item['file_name']}]({item['url']})**  \n"
                f"  _Uploadé le : {item['uploaded_at']}_",
                unsafe_allow_html=True
            )

    st.divider()

    # ---------- Dernières saisies (côté utilisateur) ----------
    st.subheader("Mes dernières saisies")
    data = fetch_last_updates(sb, user_id)
    if not data:
        st.caption("Aucune saisie trouvée.")
    else:
        for r in data:
            created = r.get("created_at", "")
            pt = r.get("progress_travaux", "")
            pp = r.get("progress_paiements", "")
            com = r.get("commentaires") or ""
            st.write(f"- **{created}** — travaux {pt}%, paiements {pp}%\n  {com}")


# =========================
# Main
# =========================
def main():
    try:
        sb = get_supabase()
    except Exception as e:
        st.error(str(e))
        return

    # Bandeaux de contrôle
    healthcheck(sb)

    # Auth
    auth_panel(sb)

    # Corps
    user_id = st.session_state.get("user_id") or get_user_id(sb)
    if user_id and user_id != st.session_state.get("user_id"):
        st.session_state["user_id"] = user_id

    st.write("")  # espacement
    if not user_id:
        st.info("Connecte-toi pour saisir une mise à jour.")
        return

    # Page "Nouvelle mise à jour" (inclut PV de chantier)
    page_nouvelle_mise_a_jour(sb, user_id)


if __name__ == "__main__":
    main()
