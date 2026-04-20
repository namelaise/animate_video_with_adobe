# segments_processing.py
# -*- coding: utf-8 -*-
"""
Outils post-traitement diarisation :
- Réécriture des SPEAKER -> "Mr Martin" / "Intervenant N" via GPT
- Parsing d'un fichier de segments au format [start → end] SPEAKER : texte
- Découpe du MP3 source en un MP3 par segment (ffmpeg), + manifeste JSON

Prérequis :
  pip install openai pydub python-dotenv
  # ffmpeg doit être installé et présent dans le PATH système
"""

from __future__ import annotations
import os
import re
import json
import logging
import subprocess
from typing import List, Dict, Optional

from pydub import AudioSegment
from openai import OpenAI

log = logging.getLogger("pipeline")


# ─────────────────────────────────────────────
# Claude CLI helper
# ─────────────────────────────────────────────

def _call_claude_cli(prompt: str, timeout: int = 180) -> str:
    """
    Appelle Claude CLI (couvert par l'abonnement).
    Lève RuntimeError si le CLI n'est pas disponible ou retourne une erreur.
    """
    result = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "text"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI erreur (code {result.returncode}): {result.stderr.strip()[:300]}")
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("Claude CLI: réponse vide")
    return output


# -----------------------------
# Expressions régulières & util
# -----------------------------

LIGNE_SEGMENT_REGEX = re.compile(
    r'^\[(?P<debut>\d+(?:\.\d+)?)\s*→\s*(?P<fin>\d+(?:\.\d+)?)\]\s*(?P<locuteur>[^:]+)\s*:\s*(?P<texte>.*)$'
)

def _slugify_nom(nom: str) -> str:
    propre = nom.strip().lower()
    propre = re.sub(r'[^a-z0-9]+', '-', propre)
    propre = re.sub(r'-{2,}', '-', propre).strip('-')
    return propre or "speaker"

def _clamp(valeur: int, borne_min: int, borne_max: int) -> int:
    return max(borne_min, min(borne_max, valeur))


# ---------------------------------------------
# 1) Réécriture des noms de locuteurs via GPT
# ---------------------------------------------

def rewrite_transcript_with_intervenants_gpt(
    contenu_segments_brut: str,
    dossier_sortie: str,
    nom_fichier_sortie: str = "transcription_segments_intervenants.txt",
    modele_openai: Optional[str] = None,
) -> str:
    """
    Réécrit le transcript diarizé en remplaçant SPKA/B/C… par :
      - 'Mr Martin' (exactement cette chaîne) pour l'auteur du canular
      - 'homme N' ou 'femme N' (N=1,2,3…) pour les autres, numérotés par ordre d'apparition
        *la numérotation est indépendante par genre* (premier homme → 'homme 1', première femme → 'femme 1').
    Ne modifie JAMAIS timestamps, texte, ponctuation, ordre/compte des lignes.
    Écrit la sortie dans `dossier_sortie/nom_fichier_sortie` et retourne le chemin.
    """

    PROMPT_SYSTEM = (
        "Tu es un transformateur de texte ULTRA STRICT. "
        "Tu réécris un fichier de dialogues diarisation sans RIEN changer "
        "à part le nom du locuteur. "
        "Tu es DÉTERMINISTE : à instructions identiques, ta sortie doit rester identique."
    )

    PROMPT_USER = f"""
    Tu vas réécrire le fichier suivant, SANS RIEN MODIFIER hormis le nom du locuteur.

    Format de chaque ligne (ne surtout pas changer) :
    [START → END] SPEAKER : TEXTE

    Règles OBLIGATOIRES :
    1) Remplace chaque SPEAKER (SPKA, SPKB, SPKC, …) par exactement l’un des formats :
    - Mr Martin
    - homme N
    - femme N
    où N = 1, 2, 3, … (numérotation par ORDRE D’APPARITION et SÉPARÉMENT pour chaque genre).
    Exemple : premières apparitions successives → ‘homme 1’, ‘femme 1’, ‘femme 2’, ‘homme 2’, etc.
    ⚠️ IMPORTANT : chaque **nouvelle personne** doit créer un nouveau numéro, même si la diarisation a réutilisé un SPEAKER déjà existant.
    Les étiquettes SPKA, SPKB… sont uniquement indicatives et NE DOIVENT PAS limiter ton raisonnement.

    2) Mr Martin est TOUJOURS présent (exactement cette orthographe : "Mr Martin").
    Identifie-le par le contexte global : c’est l’auteur du canular, la voix qui mène/provoque/relance,
    propose des idées absurdes (ex: couper l’électricité, vendre un groupe électrogène, solutions farfelues),
    se présente comme l’agent/technicien/organisateur à l’origine de l’appel, ou orchestre la situation.
    Si une réplique est ambiguë localement, favorise la COHÉRENCE GLOBALE du rôle sur l’ensemble du fichier.

    3) Détection de NOUVEAUX INTERVENANTS (ne te fie pas qu’à la diarisation) :
    - Indices textuels explicites : « je vous passe quelqu’un », « une autre dame », « un autre monsieur »,
        « je vous passe mon collègue », « c’est pas la même dame », etc. ⇒ crée un **nouveau numéro**.
    - Changement brusque de position/ton/registre qui ne correspond pas aux interlocuteurs déjà en place ⇒ nouveau numéro.
    - Ne fusionne JAMAIS deux personnes sous le même label si le texte indique qu’elles sont différentes, même si la diarisation recycle un SPEAKER.

    4) Déduction du GENRE (hors Mr Martin) :
    - Indices explicites : « c’est pas monsieur, c’est madame », « je suis un homme », etc.
    - Appellations reçues : « Bonjour madame/monsieur », « madame », « monsieur ».
        ⚠️ Mr Martin peut volontairement inverser l’appellation : ne te laisse pas piéger par l’ironie.
    - Indices morphologiques/lexicaux (accords, prénoms).
    - Si le genre est indécidable, choisis le plus plausible et reste COHÉRENT partout.

    5) Mapping CONSTANT :
    - Un même intervenant garde EXACTEMENT le même nom partout.
    - Un nouvel interlocuteur = un **nouveau** numéro unique et définitif (pas de réutilisation rétroactive).

    6) DÉMARRAGE (dé-ambiguïsation du début) :
    - Si la 2ᵉ réplique interpelle un commerce (« C’est bien le café ? »), l’appelant/organisateur est **Mr Martin**.
    - Par défaut, si la 1ʳᵉ réplique est courte et purement phatique (« Oui, bonjour. »), elle provient du **bar** (souvent femme 1),
        sauf indice contraire fort.

    7) ANCRES DE RÔLE (OBLIGATOIRES) :
    - Toute réplique qui **organise, propose, négocie, vend, annule** (dégustations, places, tombola, « je vais vous envoyer », « je vous mets », « j’annule »)
        DOIT être étiquetée **Mr Martin**.
    - Toute mention textuelle signalant un **changement d’interlocuteur** (ex. « c’est pas la même dame ») introduit **femme N / homme N** nouveau,
        même si la diarisation réutilise le même SPEAKER.

    8) Ne modifie JAMAIS :
    - les timestamps
    - le texte après le deux-points
    - la ponctuation
    - l’ordre et le nombre de lignes
    - l’espace avant et après les deux-points

    9) AVANT DE RENDRE (auto-checklist interne, mais ne rien afficher) :
    - Toutes les répliques d’organisation/vente/annulation sont-elles bien **Mr Martin** ?
    - Chaque indice « nouvelle personne » a-t-il créé **un nouveau numéro** (homme N / femme N) ?
    - Aucun intervenant n’a-t-il changé de nom en cours de route ?
    - La cohérence globale Mr Martin = orchestrateur est-elle respectée partout ?

    10) Sors UNIQUEMENT le fichier réécrit, sans explication, sans JSON, sans balises, sans ```.

    Exemple (mini) :
    ENTRÉE
    [0.00 → 1.00] SPKA : Bonjour.
    [1.00 → 2.00] SPKB : Oui bonjour.
    [2.00 → 3.00] SPKA : Je vais vous couper l’électricité pour le climat.
    [3.00 → 4.00] SPKB : Je vous passe ma collègue.
    [4.00 → 5.00] SPKB : Allô ?

    SORTIE
    [0.00 → 1.00] Mr Martin : Bonjour.
    [1.00 → 2.00] femme 1 : Oui bonjour.
    [2.00 → 3.00] Mr Martin : Je vais vous couper l’électricité pour le climat.
    [3.00 → 4.00] femme 1 : Je vous passe ma collègue.
    [4.00 → 5.00] femme 2 : Allô ?

    === FICHIER À RÉÉCRIRE ===
    {contenu_segments_brut}
    === FIN ===
    """.strip()

    texte_modele = None

    # ── Tentative 1 : Claude CLI (couvert par l’abonnement) ───────────────────
    try:
        full_prompt = f"{PROMPT_SYSTEM}\n\n{PROMPT_USER}\n\nSors UNIQUEMENT le fichier réécrit, sans explication ni balise."
        texte_modele = _call_claude_cli(full_prompt, timeout=180)
        log.info("Réécriture intervenants via Claude CLI")
    except Exception as e:
        log.warning("Claude CLI indisponible pour réécriture intervenants (%s) → fallback OpenAI", e)

    # ── Tentative 2 : OpenAI (fallback) ──────────────────────────────────────
    if texte_modele is None:
        modele = modele_openai or os.getenv("OPENAI_MODEL", "gpt-5")
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        reponse = client.chat.completions.create(
            model=modele,
            messages=[
                {"role": "system", "content": PROMPT_SYSTEM},
                {"role": "user",   "content": PROMPT_USER},
            ],
        )
        texte_modele = (reponse.choices[0].message.content or "").strip()
        log.info("Réécriture intervenants via OpenAI (%s)", modele)

    # Si le modèle renvoie un bloc ```…```, on l’extrait proprement
    bloc = re.search(r"```(?:\w+)?\s*([\s\S]*?)\s*```", texte_modele)
    texte_rewrite = bloc.group(1).strip() if bloc else texte_modele

    os.makedirs(dossier_sortie, exist_ok=True)
    chemin_sortie = os.path.join(dossier_sortie, nom_fichier_sortie)
    with open(chemin_sortie, "w", encoding="utf-8") as f:
        f.write(texte_rewrite)

    log.info("Fichier reecrit enregistre : %s", chemin_sortie)
    return chemin_sortie

# -------------------------------------------------------
# 2) Parsing d'un fichier segments (timestamps + speaker)
# -------------------------------------------------------

def parse_diarization_segments_file(
    chemin_fichier_segments: str
) -> List[Dict[str, object]]:
    """
    Lit un fichier texte style :
      [0.01 → 0.69] Intervenant 1 : Allo ?
    Retourne une liste de dicts {start,end,speaker,text}.
    """
    segments: List[Dict[str, object]] = []

    with open(chemin_fichier_segments, "r", encoding="utf-8") as fichier:
        for numero_ligne, ligne in enumerate(fichier, start=1):
            ligne = ligne.strip()
            if not ligne:
                continue
            correspondance = LIGNE_SEGMENT_REGEX.match(ligne)
            if not correspondance:
                continue

            debut_s = float(correspondance.group("debut"))
            fin_s = float(correspondance.group("fin"))
            nom_locuteur = correspondance.group("locuteur").strip()
            texte_segment = correspondance.group("texte").strip()

            if fin_s > debut_s:
                segments.append({
                    "line_index": numero_ligne,
                    "start": debut_s,
                    "end": fin_s,
                    "speaker": nom_locuteur,
                    "text": texte_segment
                })

    return segments


# ----------------------------------------------------------
# 3) Découpe MP3 selon les segments (ffmpeg) + manifeste JSON
# ----------------------------------------------------------

def cut_audio_by_diarization(
    chemin_fichier_audio_mp3: str,
    chemin_fichier_segments_txt: str,
    dossier_sortie_segments_audio: str,
    padding_millisecondes: int = 80,
    duree_minimale_conservee_millisecondes: int = 250,
    preferer_copie_flux: bool = True,
) -> List[Dict[str, object]]:
    """
    Découpe `chemin_fichier_audio_mp3` (MP3) selon `chemin_fichier_segments_txt` et écrit les .mp3 dans `dossier_sortie_segments_audio`.
    Écrit aussi un manifeste JSON: dossier_sortie_segments_audio/segments_manifest.json

    preferer_copie_flux=True  => -acodec copy (rapide, précision frame MP3)
    preferer_copie_flux=False => réencodage LAME (précision maximale)
    """
    os.makedirs(dossier_sortie_segments_audio, exist_ok=True)

    # Nettoyer d'anciens segments .mp3
    for nom in os.listdir(dossier_sortie_segments_audio):
        if nom.lower().endswith(".mp3"):
            try:
                os.remove(os.path.join(dossier_sortie_segments_audio, nom))
            except Exception:
                pass

    # Durée totale (pour clamp)
    try:
        audio_complet = AudioSegment.from_file(chemin_fichier_audio_mp3)
        duree_totale_ms = len(audio_complet)
    except Exception as exc:
        raise RuntimeError(f"Impossible de lire l'audio source: {exc}")

    segments = parse_diarization_segments_file(chemin_fichier_segments_txt)
    if not segments:
        log.warning("Aucun segment a decouper (fichier segments vide ou mal forme).")
        return []

    manifeste: List[Dict[str, object]] = []

    for segment in segments:
        line_idx = segment["line_index"]
        debut_ms = int(segment["start"] * 1000) - padding_millisecondes
        fin_ms = int(segment["end"] * 1000) + padding_millisecondes

        debut_ms = _clamp(debut_ms, 0, duree_totale_ms)
        fin_ms = _clamp(fin_ms, 0, duree_totale_ms)
        duree_ms = fin_ms - debut_ms
        if duree_ms < duree_minimale_conservee_millisecondes:
            log.warning("Segment ligne %d (%s): duree %dms < %dms, skip.", line_idx, segment['speaker'], duree_ms, duree_minimale_conservee_millisecondes)
            continue

        nom_locuteur_slug = _slugify_nom(str(segment["speaker"]))
        nom_fichier_sortie = f"seg_{line_idx:03d}_{nom_locuteur_slug}.mp3"
        chemin_sortie_segment = os.path.join(dossier_sortie_segments_audio, nom_fichier_sortie)

        # ffmpeg : -ss + -t
        debut_sec = debut_ms / 1000.0
        duree_sec = duree_ms / 1000.0

        if preferer_copie_flux:
            cmd = [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", f"{debut_sec:.3f}", "-t", f"{duree_sec:.3f}",
                "-i", chemin_fichier_audio_mp3,
                "-vn", "-acodec", "copy",
                "-y", chemin_sortie_segment
            ]
        else:
            cmd = [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", f"{debut_sec:.3f}", "-t", f"{duree_sec:.3f}",
                "-i", chemin_fichier_audio_mp3,
                "-vn", "-acodec", "libmp3lame", "-q:a", "2",
                "-y", chemin_sortie_segment
            ]

        resultat = subprocess.run(cmd)
        if resultat.returncode != 0 or not os.path.exists(chemin_sortie_segment):
            # Fallback réencodage si la copie flux a loupé
            cmd_fallback = [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", f"{debut_sec:.3f}", "-t", f"{duree_sec:.3f}",
                "-i", chemin_fichier_audio_mp3,
                "-vn", "-acodec", "libmp3lame", "-q:a", "2",
                "-y", chemin_sortie_segment
            ]
            subprocess.run(cmd_fallback, check=False)

        manifeste.append({
            "index": line_idx,
            "file": chemin_sortie_segment,
            "speaker": segment["speaker"],
            "start": round(float(segment["start"]), 3),
            "end": round(float(segment["end"]), 3),
            "text": segment["text"],
        })

    chemin_manifeste = os.path.join(dossier_sortie_segments_audio, "segments_manifest.json")
    with open(chemin_manifeste, "w", encoding="utf-8") as f:
        json.dump(manifeste, f, ensure_ascii=False, indent=2)

    log.info("%d segments exportes dans %s", len(manifeste), dossier_sortie_segments_audio)
    log.info("Manifeste: %s", chemin_manifeste)
    return manifeste


# ----------------------------------------------------------
# 4) (Optionnel) Orchestrateur simple pour enchaîner les deux
# ----------------------------------------------------------

def rewrite_and_cut_pipeline(
    chemin_fichier_audio_mp3: str,
    contenu_segments_brut: str,
    dossier_segments_audio: str,
    dossier_rewrite_sortie: str,
    nom_fichier_rewritten: str = "transcription_segments_intervenants.txt",
    preferer_copie_flux: bool = True,
) -> Dict[str, Optional[str]]:
    """
    Enchaîne :
      - réécriture des SPEAKER -> Mr Martin / Intervenant N
      - découpe MP3 selon ce nouveau fichier
    Retourne les chemins utiles.
    """
    chemin_rewrite = rewrite_transcript_with_intervenants_gpt(
        contenu_segments_brut=contenu_segments_brut,
        dossier_sortie=dossier_rewrite_sortie,
        nom_fichier_sortie=nom_fichier_rewritten,
    )

    cut_audio_by_diarization(
        chemin_fichier_audio_mp3=chemin_fichier_audio_mp3,
        chemin_fichier_segments_txt=chemin_rewrite,
        dossier_sortie_segments_audio=dossier_segments_audio,
        preferer_copie_flux=preferer_copie_flux,
    )

    return {
        "fichier_segments_intervenants": chemin_rewrite,
        "dossier_segments_audio": dossier_segments_audio,
    }
