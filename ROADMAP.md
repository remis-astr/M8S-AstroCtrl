# Feuille de route — M8S Pro L en contrôleur d'astrophoto (guidage + plate solving)

Contexte complet, architecture cible et étapes pour transformer le boîtier
MECOOL M8S Pro L (Amlogic S912) en contrôleur headless reliant la monture
OnStepX (carte FYSETC E4) et la caméra maison **minicam** (RPi Zero 2W,
projet `~/minicam_program`), piloté depuis la page HTML déjà existante du
RPi0.

## Contraintes de départ

- M8S Pro L : 2 ports USB 2.0 seulement, Ethernet inutilisable (télescope
  au jardin), **WiFi interne mort** (chip Broadcom/AP6255 sans driver
  mainline ni sur Armbian ni sur ophub — confirmé sur les forums, pas la
  peine d'insister dessus).
- OnStepX tourne sur une carte **FYSETC E4** (ESP32, WiFi natif à la carte)
  — pont USB-série intégré, confirmé **CH340** (`/dev/ttyUSB0`). Le
  **115200 bauds documenté sur le wiki FYSETC concerne le flashage du
  firmware**, pas la communication runtime : en fonctionnement normal,
  OnStepX répond au protocole LX200 à **9600 bauds** (confirmé : `:GD#` →
  `+48*18:01#`, format valide). `:GR#` renvoie `0` pour l'instant — la
  monture n'a probablement pas encore de position de référence/tracking
  actif, à surveiller mais non bloquant.
- Caméra **minicam** (RPi Zero 2W) : app FastAPI maison, capteur IMX327 ou
  IMX477, mode API custom (preview/live-stack/lucky-stack en WebGPU) **et**
  mode INDI intégré (`indi_pylibcamera` sur port 7624, exclusif du mode
  API). Réseau **double** : WiFi (AP `AllskyCam` ou client via
  NetworkManager) + **gadget USB RNDIS/ECM** (192.168.7.x, DHCP côté Pi) —
  ce lien USB est déjà testé et fonctionnel (voir permissions
  `.claude/settings.local.json` du projet minicam).
- Deux unités physiques existent avec ce logiciel : `rpi0` (Minicam,
  192.168.7.2, version antérieure du code) et **`allsky`** (192.168.7.3,
  version complète). **Décision utilisateur : c'est `allsky` qui sert de
  caméra de guidage** — confirmé avoir le mode INDI à jour (code déjà
  vérifié dans le backup local `~/minicam_program`, qui correspond à
  cette unité). `rpi0`/Minicam reste disponible pour un usage séparé,
  hors de ce projet.

## Architecture cible

```
        M8S Pro L (Armbian / Ubuntu arm64)
   ┌──────────────────────────┐
   │ Port USB #1 ──────────────┼── câble USB-série ──► FYSETC E4 (OnStepX)
   │                           │     /dev/ttyUSB0 @ 115200 (cp210x/ch341)
   │                           │
   │ Port USB #2 ──────────────┼── câble USB (data) ──► RPi0 W2 « allsky »
   │                           │     réseau RNDIS/ECM, 192.168.7.3
   └──────────────────────────┘
              │
     indiserver + indi_lx200_OnStep (série)
     ASTAP (plate solving sur FITS tiré de allsky)
     petite API FastAPI de commande (nouvelle)
              ▲
              │ HTTP sur 192.168.7.3
     RPi0 « allsky » ── WiFi (AP `AllskyAP`) ── téléphone / laptop (page HTML existante)
```

Aucune des deux liaisons M8S ne dépend du WiFi interne du boîtier — il
reste hors-jeu en permanence. Le WiFi de `allsky` ne sert qu'à afficher la
page de contrôle, jamais à parler au M8S (ce lien passe en USB).

## Phase 1 — Installer Linux sur le M8S

- [x] Sauvegarde Android stock : **volontairement non faite** — décision
      explicite de l'utilisateur, Android n'est plus nécessaire sur ce
      boîtier.
- [x] Récupérer une image [ophub/amlogic-s9xxx-armbian](https://github.com/ophub/amlogic-s9xxx-armbian)
      — build spécifique retenue : `Armbian_26.11.0_amlogic_s912-m8s-pro_bookworm_6.12.109_server_2026.09.14.img.gz`
      (checksum SHA256 vérifié avant écriture).
- [x] Écrite sur la microSD par `dd` (image déjà préconfigurée avec le dtb
      `meson-gxm-q201.dtb` dans `/boot/uEnv.txt` — repli si besoin :
      `meson-gxm-q200.dtb`).
- [x] **Pas de bouton reset physique accessible sur cette carte** (variante
      `LP_S912_V1.0` / `BB-ROC180715PL 3G+16G` / `M8S PRO L S912 3G 16G
      DDR3 LB`) — le connecteur AV n'a pas de switch caché contrairement à
      d'autres révisions. Bouton de boot forcé retrouvé sous forme
      d'empreinte non soudée **`2SW4`** (4 pastilles) en bas à droite du
      PCB, près du composant `1D5`.
      **Procédure confirmée qui fonctionne** : boîtier hors tension, carte
      SD insérée, shunter simultanément **les deux petites pastilles ET
      les deux grandes pastilles** de `2SW4` (donc les 4 ensemble, pas
      juste une paire de contact) au moment de rebrancher l'alimentation,
      maintenir quelques secondes puis relâcher.
- [x] Vérifier `lsusb`, `ip a`, accès SSH — **confirmé** : boot Armbian OK
      (`hostnamectl` → Armbian OS 26.11.0 bookworm, kernel 6.12.109-ophub,
      arm64), accessible en SSH via `eth0` sur le réseau local (IP DHCP,
      identifiée par sa signature OpenSSH parmi les hôtes du LAN).
      ⚠️ **À faire immédiatement** : le compte `root`/`1234` par défaut
      n'a pas forcé de changement de mot de passe au premier login —
      changer le mot de passe avant de laisser la carte sur le réseau.
- [x] Migration sur l'eMMC via `armbian-install` (modèle auto-détecté ID
      `206` = MECOOL-M8S-Pro-L, ext4, u-boot non-mainline) — **réussie**.
      Le boîtier boote maintenant directement sur Armbian depuis l'eMMC,
      SD retirée, **plus besoin du shunt `2SW4`** à chaque démarrage.
- [ ] Ignorer complètement le WiFi interne (`wlan0` restera non
      fonctionnel, c'est attendu).

## Phase 2 — Brancher et vérifier les deux liaisons USB

- [x] FYSETC E4 branchée → `/dev/ttyUSB0` (CH340) apparu. Communication
      série confirmée à **9600 bauds** (pas 115200) : `:GD#` → réponse
      LX200 valide. `python3-serial` installé sur le M8S pour ces tests.
- [x] RPi0 branché → interface `usb0` apparue côté M8S (`cdc_ether`,
      device « MiniCam USB »), IP `192.168.7.15/24` obtenue en DHCP.
- [x] Accès HTTP confirmé : `curl http://192.168.7.3:8000/` → HTTP 200,
      `<title>MiniCam</title>`.
      **Confirmé par l'utilisateur** : c'est bien `allsky` (`.3`,
      `AllskyAP`) qui sert de caméra de guidage — pas besoin de vérifier
      `rpi0`/Minicam plus loin.

## Phase 3 — INDI + ASTAP sur le M8S

- [x] **Correction importante** : le PPA `mutlaqja/ppa` est Ubuntu-only
      (noble/jammy/resolute), incompatible avec cette base **Debian
      bookworm** (Armbian ophub). Pas besoin de toute façon : Debian
      bookworm packages déjà `indi-bin` (v1.9.9+dfsg) **avec
      `indi_lx200_OnStep` inclus**. `apt-get install indi-bin` a suffi.
      (`gsc` n'existe pas dans les dépôts Debian — non installé, pas
      nécessaire pour notre usage ASTAP.)
- [x] `indiserver -v indi_lx200_OnStep` testé : port/baud auto-détectés
      (`/dev/ttyUSB0`, 9600), connexion réussie (`CONNECT=On`), RA/DEC
      cohérents avec le test série manuel (`DEC≈48.30°`).
- [x] ASTAP installé (`astap_aarch64_gtk3.deb`) — **le binaire GUI `astap`
      ne fonctionne pas** (dépendance `libgtk-3.so.0` absente sur ce
      serveur headless, normal/attendu) mais **`astap_cli`** (dans le même
      paquet, `/opt/astap/astap_cli`) tourne parfaitement sans dépendance
      graphique — c'est celui-là qu'on utilise de toute façon pour
      l'automatisation.
- [x] Base d'étoiles : **D50** installée (`d50_star_database.deb`, 866 Mo,
      11 Go encore libres sur l'eMMC après). Choisie après calcul pour la
      focale réelle de la lunette chercheuse (177 mm + capteur IMX327) :
      FOV ≈ 1,8°×1,0° (diagonale 2,09°) → confortablement dans la plage
      D50 (0,2°-6°), pas besoin de D80. H18 aurait été inadapté ici (H18
      n'est optimal que sous 1° de champ, ce qui correspondrait à une
      focale > ~350 mm avec ce capteur).
- [x] Test bout en bout : capture réelle via `curl
      http://192.168.7.3:8000/capture.fits` (allsky) → `astap_cli -f
      test.fits -fov 2.09 -r 30` → chaîne technique validée (moins d'1s,
      bons fichiers `.log`/`.ini` générés). « 0 étoile trouvée » attendu
      (image de test de jour/à l'établi) — vrai test de solve sous le ciel
      prévu en Phase 6.

## Phase 4 — Service de commande sur le M8S — TERMINÉ

- [x] `python3-fastapi` + `python3-uvicorn` installés (paquets Debian
      natifs, pas besoin de pip/venv).
- [x] API écrite dans `/opt/m8s-ctrl/main.py`, pilotage via
      `indi_setprop`/`indi_getprop` (CLI, pas de dépendance
      `pyindi-client`) :
      - `GET /status` — connexion, tracking, RA/DEC, dernier solve
      - `POST /mount/track` `{"action": "start"|"stop"}`
      - `POST /mount/slew` `{"ra_h": ..., "dec_deg": ...}`
      - `POST /solve` — capture FITS sur allsky → `astap_cli` → sync
        monture si résolu
- [x] Deux services systemd créés et activés au boot :
      - `indi-onstep.service` (indiserver + `indi_lx200_OnStep`)
      - `m8s-ctrl.service` (l'API, port 8080, `Requires=indi-onstep`)
- [x] Test bout en bout : `/status` fonctionne, `/mount/track` répond
      `{"ok": true}` et le protocole INDI accepte la requête (log
      `-vv` confirme `newSwitchVector TRACK_ON=On` bien reçu par le
      driver) — **mais sans effet réel confirmé sur la monture** :
      ⚠️ **Erreur de diagnostic corrigée** : j'avais conclu à tort que le
      suivi sidéral était actif en me basant sur la dérive de l'AD
      (+0,016667h/60s ≈ taux sidéral). **C'est un raisonnement invalide** —
      une monture équatoriale à l'arrêt (angle horaire fixe) voit
      naturellement son AD calculée dériver au taux sidéral, sans aucun
      mouvement moteur (AD = TSL − angle horaire ; la Dec strictement
      constante pendant le test en est la signature). **Vérifié
      directement par l'utilisateur sur OnStepX : le suivi n'est PAS
      actif.** Donc soit l'appel `TELESCOPE_TRACK_STATE.TRACK_ON=On` de ce
      driver (INDI 1.9.9, ancien) ne déclenche réellement aucune commande
      série vers la monture (le log `-vv` ne montre aucun
      `setSwitchVector` de confirmation ni de trafic série sortant après
      la requête), soit `:Te#` (testé isolément en série brut, ack `1`)
      est une commande **toggle** et non un « set ON » explicite, dont
      l'état résultant reste incertain sans confirmation indépendante.
      **À refaire en Phase 6** avec vérification visuelle directe sur
      OnStepX (pas de déduction indirecte) pendant l'appel à `/mount/track`.
      Non testé non plus : `POST /mount/slew` (jamais essayé de vrai
      slew) et `POST /solve` avec une vraie image étoilée.

## Réorientation du 24/09/2026

- **INDI abandonné** (le driver Debian lit la monture mais n'écrit rien).
  Pilotage OnStepX en LX200 série direct depuis le service du M8S.
- **Objectifs clarifiés** : autoguideur autonome façon Lacerta MGEN-3,
  en EQ **et** ALT-AZ (avec gestion de la rotation de champ), plus un
  alignement et un recalage par plate solve ASTAP.
  **Tous les détails sont dans `CONCEPTION.md`.**

## Phase 4b — Remplacer INDI par le pilotage série direct

- [x] `indi-onstep.service` supprimé, `indi-bin` purgé (24/09).
      Ancienne version sauvegardée dans `m8s-ctrl/backup-indi/`.
- [x] Module `onstep.py` (source locale : `m8s-ctrl/`, déployé dans
      `/opt/m8s-ctrl/`) :
      - port et débit détectés automatiquement (pour la E4 et le futur
        kit Terrans EQ) ;
      - connexion permanente protégée par un verrou ;
      - type de monture lu dans `:GU#` ;
      - suivi vérifié par relecture de `:GU#` après chaque commande.
      API : `/status`, `/mount/site`, `/mount/track`, `/mount/slew`,
      `/mount/sync`, `/mount/stop`, `/mount/guide`, `/solve`.
- [x] Lecture validée sur la E4 :
      - OnStepX **10.28w**, **ALTAZM**, 9600 bauds, `/status` en 0,4 s ;
      - l'ancien `0` renvoyé par `:GR#` était une **réponse d'erreur**
        (les échecs répondent `0` sans `#`).
      **Piège** : forcer DTR/RTS à False à l'ouverture empêche la E4 de
      répondre. On garde les valeurs par défaut de pyserial.
- [x] Code source d'OnStepX vérifié :
      - `:Te#`/`:Td#` sont explicites (pas une bascule) ;
      - en ALTAZM, `:Mg`/`:MG` s'appliquent **en RA/Dec** (converti en
        interne vers Az/Alt), et **seulement pendant le suivi** ;
      - en mode alignement, `:CM#` ajoute le point avec la cible
        `:Sr`/`:Sd` comme position vraie : c'est l'alignement par plate
        solve ;
      - le type de monture se change avec `:SXEM,n#` (effectif au
        redémarrage).
- [ ] **Horloge de la E4** : le 24/09, elle indiquait le 21/09 23:23
      (heure locale). Elle reste périmée tant qu'on ne s'est pas connecté
      au SWS, ce qui confirme qu'il faut une mise à l'heure depuis allsky.
      Site lu : 47°18′ N, 0°29′ E.
- [x] Tests de mouvement du 24/09 (moteurs débranchés, SWS sous les yeux
      de l'utilisateur) :
      - suivi marche/arrêt : OK, relu dans `:GU#` ;
      - impulsions N/W/S de 2 s : ±15″ sur le bon axe, soit 0,5× sidéral ;
      - GoTo : accepté, en mouvement ;
      - `stop` : arrêt en moins de 2 s.
- [x] **La E4 est la référence de temps** (décision de l'utilisateur,
      24/09) :
      - NTP du M8S désactivé (`timedatectl set-ntp false`) ;
      - l'API recale l'horloge système sur l'UTC de la E4 au démarrage
        puis toutes les 15 min, si l'écart dépasse 0,5 s. Recalage manuel :
        `POST /time/sync`. Correction testée : +5 s ramenés à 0,006 s ;
      - `/mount/site` contrôle la cohérence : écart entre M8S et E4, et
        temps sidéral de la E4 comparé à celui recalculé depuis son UTC et
        sa longitude.
      **Piège de mise à l'heure** : l'appareil utilisé pour le SWS envoyait
      une heure locale en UTC+1, ce qui a donné 1 puis 2 h d'erreur selon
      le fuseau choisi. Le réglage final est juste : UTC à 0,01 s, temps
      sidéral à 0,6 s (heure locale 1 h en retard, fuseau UTC+1).
      **Limite** : la E4 n'a pas de pile. Au démarrage, elle repart sur une
      date périmée tant que le SWS n'a pas été utilisé, et le M8S
      recopiera cette erreur. Il faut prévoir une alerte si la date de la
      E4 est antérieure à la dernière date connue.
      **FastAPI 0.92** (Debian) : le paramètre `lifespan` est ignoré sans
      erreur. Il faut utiliser `@app.on_event("startup")`.

## Phase 5 — Côté allsky (infrastructure) — 24/09

- [x] Endpoint **`GET /guide/frame`** (`api/routes_guide.py`) : RAW packé,
      géométrie dans les en-têtes `X-*`, sans écriture SD. Il donne
      **0,15 s par trame**, contre 1,5 s pour `/ws/raw`. La boucle
      d'aperçu JPEG se met en pause pendant le guidage.
- [x] Client M8S `m8s-ctrl/guidecam.py` : lecture et dépaquetage 10/12
      bits en numpy. Paquets Debian installés sur le M8S :
      `python3-numpy`, `python3-websockets`, `python3-pil`.
- [x] `Conflicts=` retirés de `minicam-net-usb` et `minicam-net-wifi`.
- [x] IP fixe du M8S sur l'USB : `192.168.7.1` (connexion NetworkManager
      `m8s-usb-allsky`).
- [x] Redirection nftables `192.168.4.1:8080` → M8S
      (`minicam-m8s-forward.service`).
- [ ] **Test du chemin depuis le téléphone** : sur le WiFi `AllskyCam`,
      ouvrir `http://192.168.4.1:8080/status`.
- [ ] Le capteur d'allsky est un **IMX477** : il faudra choisir les modes
      de guidage (`1080p` natif) et de solve (`2028x1520_bin`) sous le
      ciel (voir `CONCEPTION.md` §3).
- [ ] Boutons et état dans l'interface (`index.html`/`app.js`) : à faire
      après les phases 5b et 5c, une fois que les fonctions existent côté
      M8S.
- Les fichiers modifiés dans `~/minicam_program` ne sont **pas commités** :
      le dépôt contenait déjà d'autres modifications non commitées de
      l'utilisateur.

## Phase 5b — Alignement par plate solve (M8S) — code en place le 24/09

- [x] Heure : la E4 est la référence (voir phase 4b).
- [x] `astro.py` : précession et nutation J2000 → JNow, conversions
      Alt/Az ↔ RA/Dec, angle parallactique. Validé sur l'exemple 23.a de
      Meeus : 0,24″ d'écart, hors aberration volontairement ignorée
      (≤ 20″).
- [x] `solver.py` : trame courante d'allsky, FITS Bayer en pleine
      résolution dans `/dev/shm`, puis `astap_cli -check` avec indices de
      position et de champ. Pipeline testé : 0,5 s de capture et 0,9 s de
      solve (« Not enough stars » dans le noir).
      **Pose et gain** : ceux réglés à la main sur la page d'allsky
      (500 à 1000 ms, gain élevé), **jamais imposés par le M8S**
      (décision de l'utilisateur). Ils sont relus dans les en-têtes de la
      trame. Changer la pose le temps d'une seule trame s'est révélé
      lent et peu fiable (file de libcamera), et cette voie a été retirée.
- [x] `align.py` + API : `POST /align/start` (1 à 9 points, simulation
      possible), `GET /align/status`, `POST /align/abort`,
      `POST /solve {sync}`, `POST /center`.
      **Alignement simulé de 3 points réussi sur la E4** (moteurs
      débranchés) : OnStepX a accepté les 3 points (`:A?#` → 4/3). Durée
      totale : 329 s, dominée par les GoTo (environ 2 min chacun).
      En simulation, rien n'est écrit (pas de `:AW#`).
- [x] **Deux modes d'alignement** (24/09) :
      - **Manuel** (par défaut dans l'interface) : on vise n'importe quelle
        zone dégagée à la raquette, puis « Ajouter ce point » (solve +
        `:CM#`, sans GoTo). L'écart avec les points déjà pris est affiché,
        avec une alerte en dessous de 30°.
      - **Automatique** : points répartis dans la zone de ciel dégagé
        choisie (direction + étendue, 90° par défaut), hauteurs variées
        (35 à 65° par défaut).
      Pour un bon modèle : points écartés d'au moins 60°, hauteurs
      variées, proches de la zone à photographier. L'ancienne répartition
      à 120° et hauteur fixe est abandonnée.
      **Sortie propre du mode alignement** (`:SX09,0#`, soit
      `alignReset`) en cas d'interruption ou d'échec, sinon un recalage
      ultérieur aurait ajouté un point au modèle. Attention :
      `alignReset` efface aussi le modèle en mémoire vive, comme le fait
      déjà le démarrage d'un alignement. Testé sur la E4 : démarrage,
      raquette autorisée, `/solve` avec sync refusé, échec de solve sans
      ajout, interruption qui remet OnStepX à 0/0.
- [x] **Premier solve réel** (24/09), sur une vraie image
      (`/mnt/DisqueExterne/Images astro/20260823_053337/00000004.fits` :
      IMX327, pose de 60 s, 415 mm, NGC 281 Pacman), passée dans la chaîne
      de l'API sur le M8S :
      - **Avec indice de position : 1,0 à 1,9 s pour ASTAP** (1,2 à 2,1 s
        au total), indice décalé de 0 à 4,6°, solutions identiques à 0,2″
        près. Échelle 1,453″/px, soit **focale mesurée 411,7 mm**.
      - **À l'aveugle : environ 118 s** avec un champ proche du bon, et
        > 120 s avec le champ exact (0,43°). Le solve à l'aveugle n'est
        donc qu'un dernier recours. Délai porté à 300 s ; les délais
        dépassés donnent maintenant un « non résolu » propre.
      - Une focale mal réglée fait échouer le solve (rapidement avec un
        indice). **La focale devient un réglage** (`GET|POST /optics`,
        champ dans la page), et chaque solve réussi renvoie la focale
        mesurée, avec une alerte dans la page si l'écart dépasse 5 %.
      - L'image contenait 119 étoiles avec un HFD de 7,4 px.
- [ ] **Test réel de nuit** : solve sur le ciel, alignement réel, écart
      de pointage mesuré, centrage.
- [ ] **Décalage entre la lunette guide et l'instrument principal** : le
      modèle aligne l'axe de la *lunette guide*. Il faudra une étape pour
      mesurer ce décalage (centrer une fois une cible dans l'instrument
      principal).
- [ ] Alerte si la date de la E4 est périmée (E4 sans pile, voir 4b).

## Phase 5c — Guidage (M8S) — moteur en place le 24/09

- [x] `stars.py` : détection sur la matrice de Bayer sans binning (chaque
      canal ramené à son bruit), centroïdes, appariement, raccrochage par
      vote, ajustement de similitude (rotation + translation). Sur champ
      synthétique : centroïde à 0,06 px, pivot retrouvé à ±0,05 px même
      avec 0,8° de rotation. **Coût sur le M8S : 0,85 s par trame
      2028×1080** (optimisation possible : recherche limitée autour des
      étoiles connues).
- [x] `guider.py` :
      - **Calibration** : RA, retour, rattrapage du jeu Dec, Dec. Points
        dérotés dans le repère de référence, contrôle d'orthogonalité.
      - **Guidage multi-étoiles** au *pivot* (réglable, centre par
        défaut). Correcteurs façon PHD2 : hystérésis, agressivité,
        déplacement minimal, impulsion maximale, mode Dec.
      - **Mise à jour de la calibration** : RA selon cos(Dec) ; retournement
        au méridien GEM (hypothèse « tout inverser », à confirmer) ;
        rotation de champ ALT-AZ **mesurée et chaînée** depuis la
        calibration, avec repli sur le modèle d'angle parallactique (parité
        estimée).
      - Référence enrichie par les étoiles qui entrent dans le champ.
      - Compensation optionnelle du jeu Dec (désactivée par défaut).
      - Simulateur `SimSky` : ciel, bruit, seeing, dérive, erreur
        périodique, rotation de champ, jeu Dec.
- [x] **Résultats du simulateur** (dérive 8,5 px en 90 s et EP ±2 px sans
      guidage) :
      - EQ : pivot réel à 0,41 px en médiane, 0,27 px avec la compensation
        du jeu ;
      - ALT-AZ : 0,39 px avec 6,3° de rotation (+6,32° mesurés pour +6,34°
        réels).
- [x] API :
      - `POST /guide/calibrate`, `/guide/start`, `/guide/stop` ;
      - `GET /guide/status` (état, RMS, historique, calibration) ;
      - `GET|POST /guide/settings` ;
      - `GET /guide/preview.jpg` (vignette annotée calculée sur le M8S) ;
      - `POST /guide/simulation` (ciel simulé, calibration séparée).
      Session simulée complète validée sur le M8S par l'API :
      calibration en 2 min 13 s, RMS de 2,0″.
- [ ] **Test sous le ciel réel.**
- [x] **Indépendance vis-à-vis du mode vidéo** : coordonnées internes en
      pixels natifs du capteur, comptés depuis son centre. On peut calibrer
      en plein champ et guider en recadré, ou changer de mode pendant le
      guidage : essai simulé 2028×1080 → 848×480 en plein guidage, pivot
      à 0,38 px.
      Temps de détection sur le M8S :
      | Mode | Temps |
      |---|---|
      | 2028×1080 | 0,87 s |
      | 720p | 0,35 s |
      | 480p | 0,17 s |
- [x] **Page `guidage.html`** sur allsky (lien ajouté dans `index.html`),
      sans aucune ressource externe. Quatre sections :
      - **Monture** : état, suivi, STOP, GoTo, GoTo + centrage, alertes
        d'heure et de zénith ;
      - **Caméra guide** : mode, pose et gain via `/ws/control` d'allsky ;
      - **Plate solve et alignement** : solve, recalage, alignement à N
        points avec journal ;
      - **Autoguidage** : vignette, clic pour placer le pivot, calibration,
        départ et arrêt, RMS, courbes AD/Déc avec impulsions, réglages,
        bascule simulation.
      Testée dans Chrome via des tunnels SSH, en mode simulation :
      mise à jour de l'état, vignette annotée, courbes, RMS de 1,0″,
      pivot placé au clic.
      Les rafraîchissements se mettent en pause quand l'onglet est masqué
      (économie de batterie sur le téléphone).
- [x] **Raquette manuelle** (section Monture de `guidage.html`,
      API `/paddle/press|release|rates`) :
      - **Sécurité « homme mort »** : la page renouvelle l'appui toutes les
        250 ms, et le M8S arrête l'axe s'il ne reçoit rien pendant 0,8 s.
        OnStepX s'arrête de toute façon au bout de 10 s
        (`GUIDE_TIME_LIMIT`). Testé sur la E4 : appui abandonné arrêté en
        0,85 s.
      - **Vitesses de 2× à max seulement** : avec `:Rn#`, une vitesse ≤ 1×
        remplacerait aussi la vitesse des impulsions de guidage, ce qui
        fausserait la calibration. La vitesse manuelle d'origine (réglée
        dans SWS) est restaurée après chaque utilisation.
      - **En ALT-AZ, OnStepX déplace directement les axes** (mesuré :
        n = hauteur +, w = azimut +) ; en EQ, Déc et AD. Les libellés
        s'adaptent au type de monture.
      - Raquette refusée pendant le guidage, la calibration et
        l'alignement.
- [ ] **Test sur le téléphone** (WiFi `AllskyCam`) puis sous le ciel réel.

## Phase 6 — Tests bout en bout

**Ordre conseillé pour la première nuit** (24/09) :
1. Heure : mettre la E4 à l'heure via SWS, puis vérifier dans la page que
   l'alerte « heure incohérente » ne s'affiche pas.
2. Caméra : pose de 500 à 1000 ms, gain élevé, mode large
   (`2028x1520_bin`), focale 177 mm.
3. « Résoudre » seul (sans recalage) : noter le temps et la focale
   mesurée.
4. Alignement manuel 3 points à la raquette (≥ 60° d'écart, hauteurs
   variées), puis GoTo + centrage sur une cible : noter l'erreur
   résiduelle.
5. Calibration (suivi actif, moteurs branchés), puis guidage 15 à 30 min :
   noter le RMS, et en ALT-AZ la rotation mesurée comparée au modèle.
6. Essai d'un mode recadré pendant le guidage.
7. Placer le pivot sur l'étoile centrée dans le Newton.

- [ ] Depuis la page HTML de `allsky` (ouverte sur son WiFi `AllskyAP`) :
      démarrer le tracking, déclencher un solve, vérifier le sync monture.
- [ ] Session réelle de guidage sur le ciel — valider la boucle complète
      capture → solve → correction.
- [ ] Vérifier l'alimentation sur le terrain (le M8S en 5V/2A, `allsky` et
      la E4 ont chacun leurs propres besoins — prévoir une distribution
      12V→5V propre, pas de convertisseur bas de gamme).

## Risques / points ouverts

- Le retrait du `Conflicts=` systemd côté `allsky` est une hypothèse à
  valider en pratique (pas de raison technique connue de blocage, mais
  non testé).
- `:GR#` (ascension droite) renvoie `0` au lieu d'un format LX200 valide —
  à surveiller une fois la monture alignée/en tracking (Phase 3).
