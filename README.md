# ClipStudio

Studio local : transforme une vidéo en plusieurs clips recadrés 9:16 ou 16:9, prêts à publier.

## Lancer

**Windows : double-clique `start.bat`** — le serveur démarre dans sa propre fenêtre et le navigateur
s'ouvre sur http://127.0.0.1:8000.

Aucune installation : le projet embarque son runtime.

| Dossier | Contenu |
| --- | --- |
| `.runtime/python` | Python 3.12.8 portable + FastAPI, Uvicorn, yt-dlp |
| `.runtime/ffmpeg/bin` | `ffmpeg.exe` et `ffprobe.exe` portables |

Pour utiliser un Python système à la place (macOS/Linux ou venv) :

```bash
python -m venv .venv
source .venv/bin/activate      # Windows : .venv\Scripts\activate
pip install -r backend/requirements.txt
sudo apt install ffmpeg        # ou brew install ffmpeg
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8000
```

Le serveur sert **l'API et l'interface** sur le même port.

## Ca ne demarre pas sur un autre PC ?

`start.bat` installe tout automatiquement au premier lancement s'il manque quelque chose :
Python portable, pip, les dependances et FFmpeg (~130 Mo, internet requis, une seule fois).
Le script `setup.ps1` peut aussi etre lance seul (clic droit → Executer avec PowerShell).

| Symptome | Cause | Solution |
| --- | --- | --- |
| Fenetre qui se ferme tout de suite | `run.py` manquant ou dossier incomplet | Relancer `start.bat` depuis le dossier du projet ; ne deplacer que des fichiers, pas le `.exe` seul |
| « installation du runtime » puis echec | Pas d'internet ou telechargement bloque | Se connecter, relancer `start.bat`. Un antivirus/proxy d'entreprise peut bloquer python.org et gyan.dev : ajouter une exception |
| Page blanche / « serveur injoignable » | Le serveur n'a pas demarre | Lire la fenetre noire : l'erreur exacte y est affichee |
| Le port 8000 est deja utilise | Autre logiciel (ou une ancienne instance) | Rien a faire : `run.py` prend le port libre suivant (8001, 8002…) et affiche l'URL exacte |
| « ffmpeg est introuvable » | Runtime incomplet | Relancer `setup.ps1` |
| Windows Defender / SmartScreen bloque | `.exe` telecharges | Autoriser, ou relancer `setup.ps1` qui retire la marque "bloque" des `.exe` |
| Decoupe qui echoue sur un chemin bizarre | Chemin tres long ou dossier synchronise | Deplacer le projet dans `C:\ClipStudio` (eviter OneDrive et les chemins > 200 caracteres) |

Si `.runtime` est copie depuis un autre PC : cela fonctionne, il est autonome. Sinon, laisser
`start.bat` le recreer. Pour voir l'erreur reelle, ouvrir une invite de commandes **dans le dossier
du projet** et lancer :

```bat
.runtime\python\python.exe run.py
```

## Diagnostic automatique

Si le demarrage echoue, `run.py` ecrit **`diagnostic.txt`** a la racine du projet : version de
Python et de Windows, fichiers manquants, modules Python absents, presence et version de
`ffmpeg`/`ffprobe`, ports occupes entre 8000 et 8024 (avec le PID), droits d'ecriture dans
`backend/data`, espace disque et erreur complete. C'est tout ce qu'il faut pour diagnostiquer
a distance : il suffit d'envoyer ce fichier.

On peut aussi le generer a la demande :

```bat
.runtime\python\python.exe run.py --diag
```

`LISEZMOI.txt` contient les 3 etapes a suivre par une personne qui decouvre le projet.

## Sous-titres automatiques

Case **Sous-titres automatiques** dans les réglages. La voix est transcrite **en local**
(faster-whisper, aucune requête vers un service distant), mise en cache par vidéo, puis
incrustée au moment de l'encodage dans un fichier `.ass` : gros texte blanc, contour noir,
deux lignes maximum.

La **position** se règle en **glissant le sous-titre sur l'aperçu 9:16** affiché sous la case
(ou en cliquant à l'endroit voulu, ou avec les flèches ↑ ↓ du clavier). Le texte reste
centré horizontalement ; la bande du bas, grisée « interface TikTok », est interdite pour
que la légende et les boutons de l'application ne recouvrent jamais le texte. La position
(78 % par défaut) est mémorisée et appliquée à tous les clips de la génération.

- Modèle `base` par défaut (~140 Mo, téléchargé une seule fois dans `.runtime/models`).
  Pour une meilleure qualité : `set CLIPSTUDIO_WHISPER_MODEL=small` avant `start.bat`.
- L'audio est décodé par **ffmpeg** (16 kHz mono, float32) puis passé à Whisper : pas de
dépendance PyAV fragile, et un seul décodage pour toute la vidéo.
- La transcription sert aussi à choisir les moments forts : elle n'est faite qu'une fois.
- Sans `faster-whisper`, la case est grisée et indique de relancer `setup.ps1` (étape 5,
facultative : le reste de l'application fonctionne sans).

## Publication automatique (YouTube / TikTok)

Bloc **Publication automatique** dans les réglages : connecte une plateforme une fois, puis
coche *Publier dès que les clips sont prêts* (ou clique **Publier maintenant**). Chaque clip
est envoyé dès qu'il apparaît, sans passer par le site. Les jetons OAuth vivent dans
`backend/data/publish.json` et ne sont jamais renvoyés au navigateur.

### YouTube (fonctionne en local)

1. Sur https://console.cloud.google.com, crée un projet puis active **YouTube Data API v3**.
2. **Identifiants → Créer → ID client OAuth**, type **Application de bureau**.
   (Un client *Application Web* marche aussi si tu déclares exactement l'URL affichée dans
   l'interface : `http://127.0.0.1:8000/api/oauth/youtube/callback`.)
3. Colle **Client ID** + **Client Secret** dans l'interface, **Enregistrer**, puis **Connecter**.
   Si l'app OAuth est en mode *Test*, ajoute ton compte Google dans *Utilisateurs test*.
4. Choisis la visibilité : **privé** (par défaut), **non répertorié** ou **public**.

L'envoi utilise le protocole *resumable* officiel (`uploadType=resumable`, puis `PUT` du
fichier sur l'URL de session), portée `youtube.upload`. Quota par défaut : **100 envois par
jour et par projet** (minuit, heure du Pacifique).

### TikTok (limites imposées par TikTok)

Même principe (Content Posting API, `video.publish`, envoi `FILE_UPLOAD` par morceaux), mais
TikTok est plus strict — à savoir avant de créer l'app :

- L'URL de redirection doit être **enregistrée dans l'app** et TikTok **refuse `localhost`**
  pour un client Web. Il faut donc soit un client *Desktop*, soit un domaine HTTPS qui
  pointe vers cette machine (`http://127.0.0.1:8000/api/oauth/tiktok/callback` n'est accepté
  que si l'app l'autorise).
- Une app **non auditée** ne publie qu'en **privé** (`SELF_ONLY`, forcé côté serveur) et
  reste limitée à **5 utilisateurs par 24 h**. La publication publique exige l'audit TikTok.

Sans app TikTok validée, YouTube est la voie utilisable immédiatement.

## Recadrage caméra + gameplay

Réglage **Recadrage** → *Cam + gameplay* : deux bandes de 1080x960 empilées (caméra en haut,
jeu en bas) sur une sortie 1080x1920.

- **Tout se fait en glissant** : une image de la source s'affiche sous les réglages, avec un
  cadre dessus. Glisse le cadre sur l'incrustation caméra (ou clique ailleurs pour l'y
  amener), tire le coin pour la taille. Aucun champ de coordonnées, aucun coin imposé :
  le cadre garde le ratio de la bande (1080x960) et correspond exactement à la zone exportée.
  L'aperçu du bas se met à jour **pendant** le glissement, sans attendre de relâcher.
- **Détecter** propose une position automatiquement (bords rectangulaires stables sur une
  moyenne d'images). Si la détection n'est pas sûre, le cadre n'est qu'un point de départ
  et le message le dit clairement : il suffit de le déplacer.
- La bande gameplay est le plus grand cadre possible qui **évite la caméra**. Avec une
  caméra au centre de l'image, elle ne peut pas toujours l'éviter complètement : l'aperçu
  le montre avant d'encoder.
- **Aperçu réel** avant génération : `GET /api/preview/{source_id}?layout=split&x=…&y=…&w=…`
  renvoie une image 1080x1920 du rendu final, `GET /api/frame/{source_id}` l'image source
  qui sert à placer le cadre, et `GET /api/cam/{source_id}` le résultat de la détection
  (`detected` si elle est sûre, `candidate` sinon).
- Le cadre est envoyé à l'encodage dans `POST /api/process` via `cam_box: {x, y, w}` (fractions
  de la source) ; `cam_corner` / `cam_scale` restent acceptés comme solution de repli.
- Réservé au vertical 9:16 (un message le signale pour le 16:9).

## Utilisation

1. **Fichier** : dépose un MP4, MOV, MKV ou WebM (jusqu'à 3 Go).
2. **Lien** : colle une URL YouTube, Twitch ou Kick publique (téléchargement via yt-dlp ;
   VOD et clips Kick : `kick.com/chaîne/videos/<uuid>`, `kick.com/chaîne?clip=clip_…`).
3. **Clips Twitch** : liste les clips d'une chaîne et importe celui qui t'intéresse.
4. Choisis le nombre de clips (curseur de 1 à 20), la durée et le format, puis génère.
   Chaque clip apparaît dans la liste dès qu'il est encodé, sans attendre les autres.
5. Télécharge clip par clip ou tout en ZIP. `Ctrl` + `Entrée` lance la génération.

Sans `faster-whisper`, les passages sont répartis dans la vidéo. Avec :

```bash
.runtime/python/python.exe -m pip install faster-whisper
```
La détection utilise alors la transcription (mots-clés, ponctuation, densité de parole) et le
modèle reste en mémoire entre deux traitements. Le premier lancement télécharge le modèle.

## Clips Twitch

ClipStudio interroge l'API Helix de Twitch avec un **app access token** (aucun compte utilisateur requis).

1. Ouvre https://dev.twitch.tv/console/apps → **Register your application**.
2. Type d'application : **Confidential**. URL OAuth : `http://localhost` suffit.
3. Copie le **Client ID** et génère un **Client Secret**.
4. Dans l'onglet « Clips Twitch », colle-les et clique **Enregistrer**.

Les identifiants sont validés auprès de Twitch puis stockés dans `backend/data/twitch.json`
(jamais renvoyés au navigateur). Ils peuvent aussi venir de l'environnement :

```bash
TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=...
```
Les variables d'environnement sont prioritaires sur le fichier.

Ensuite : ajoute une chaîne par son nom (`kameto`, ou une URL de chaîne), choisis la période
(24 h, 7 jours, 30 jours, tout) et clique **Découper** sur un clip : le MP4 est téléchargé comme
source, prêt à être découpé avec les réglages habituels.

## Stockage et purge

Tout est écrit dans `backend/data` :

| Dossier | Contenu |
| --- | --- |
| `uploads/` | vidéos importées (fichier ou Twitch) |
| `outputs/` | clips encodés, vignettes de clips et ZIP |
| `thumbs/` | vignettes de la vidéo source |

Le badge **Stockage** de la barre du haut affiche l'espace occupé et ouvre la fenêtre de purge :
coche les catégories à supprimer, choisis éventuellement « de plus d'un jour / 7 jours / 30 jours »,
puis **Purger la sélection**. La purge est refusée pendant un encodage. Supprimer les imports remet
l'interface sur l'écran d'accueil (la source courante n'existe plus).

## Architecture

```
backend/app.py      API FastAPI + service de l'interface et des médias
frontend/           index.html · app.js · styles.css (aucune étape de build)
.runtime/           Python et FFmpeg portables
backend/data/       uploads/, outputs/, thumbs/, channels.json, twitch.json, publish.json
```

### API

| Route | Rôle |
| --- | --- |
| `GET /api/health` | présence de ffmpeg, ffprobe, yt-dlp, whisper |
| `POST /api/upload` | corps brut (`application/octet-stream`) + en-tête `X-Filename` |
| `POST /api/url` | import YouTube / Twitch / Kick via yt-dlp |
| `POST /api/process` | démarre une tâche, renvoie `{ job_id }` |
| `GET /api/jobs/{id}` | état, progression, clips, ZIP |
| `GET /api/twitch/status` | identifiants présents/valides + chaînes enregistrées |
| `POST /api/twitch/credentials` | enregistre (et valide) Client ID / Client Secret |
| `DELETE /api/twitch/credentials` | oublie les identifiants du fichier local |
| `POST /api/twitch/channels` | ajoute une chaîne (`{ "login": "..." }`) |
| `DELETE /api/twitch/channels/{login}` | retire une chaîne |
| `GET /api/twitch/clips/{login}?period=week` | clips Helix (thumb, vues, durée, `mp4`) |
| `POST /api/twitch/import` | télécharge un clip Twitch comme source |
| `GET /api/publish/status` | plateformes configurées/connectées (YouTube, TikTok) |
| `POST /api/publish/credentials/{provider}` | enregistre Client ID / Client Secret |
| `DELETE /api/publish/credentials/{provider}` | oublie identifiants et jeton |
| `POST /api/publish/auth/{provider}` | renvoie l'URL d'autorisation OAuth à ouvrir |
| `GET /api/oauth/{provider}/callback` | retour OAuth : échange le code et stocke le jeton |
| `POST /api/publish/{job_id}` | publie les clips d'une tâche (`{ provider, privacy }`) |
| `GET /api/publish/{job_id}` | état d'envoi clip par clip |
| `GET /api/storage` | octets et nombre de fichiers par dossier |
| `POST /api/storage/purge` | supprime `uploads` / `outputs` / `thumbs`, filtrable par âge |
| `/sources`, `/thumbs`, `/media` | vidéo source, vignettes, clips (avec support Range) |

## Ce qui a été optimisé

- **Import** : corps de requête écrit en flux direct dans le fichier final — plus de fichier
  temporaire multipart ni de lecture par blocs de 1 Mo. Mesuré : **64 Mo en 0,88 s**, avec une vraie
  barre de progression.
- **Sondage vidéo** : durée + résolution en un seul `ffprobe`.
- **Génération** : tâche de fond avec progression réelle, encodage de plusieurs clips en parallèle
  selon les cœurs, vignettes extraites par FFmpeg, nombre de clips plafonné par la durée disponible.
- **Clips au fur et à mesure** : `GET /api/jobs/{id}` publie chaque clip dès qu'il est écrit — la
  liste `clips` grandit pendant que le statut est encore `running`, et l'interface n'ajoute que les
  nouveaux (les clips déjà affichés ne sont pas rechargés).
- **Lecteur source** : la vidéo s'adapte à la colonne (100 % de largeur, `object-fit: contain`),
  sans débordement ni déformation.
- **Lecture** : fichiers servis par `StaticFiles` (seek et prévisualisation sans téléchargement).
- **Stockage** : purge manuelle par catégorie et par ancienneté.

## Limites / sécurité

- Utilise uniquement des vidéos que tu possèdes ou pour lesquelles tu as l'autorisation de faire des extraits.
- Ne contourne pas les DRM, connexions privées, paywalls ou restrictions d'accès.
- Serveur prévu pour un usage local : ne l'expose pas sur Internet sans authentification et quotas.
- `backend/data/twitch.json` contient un secret en clair sur ton disque : ne le partage pas.
