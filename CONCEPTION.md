# Conception — autoguideur M8S + allsky (objectifs clarifiés le 24/09/2026)

Document de référence des objectifs et des choix techniques. `ROADMAP.md`
reste le suivi d'avancement phase par phase.

## 1. Objectif

Construire un **autoguideur autonome**, sur le modèle de la Lacerta MGEN-3
(boîtier dédié : calibration, guidage, courbes d'erreur, sans PC), avec
deux différences :

1. **Le guidage fonctionne en EQ ou en ALT-AZ**, avec une gestion de la
   rotation de champ en ALT-AZ.
2. **Un plate solving ASTAP est intégré** pour aligner OnStepX sur 1 ou
   plusieurs points en quelques secondes par point, sans viser d'étoile
   particulière. Il sert aussi à recaler ponctuellement les coordonnées
   (sync).

Il n'y a **pas d'INDI** (décision du 24/09). Le M8S parle LX200 en série à
OnStepX.

## 2. Répartition des rôles

| Élément | Rôle | Principe |
|---|---|---|
| **allsky** (RPi0 2W) | Capteur d'images **et** serveur de la page HTML | Le Pi capture et sert des octets bruts, sans aucun calcul d'image. |
| **M8S** (S912, 3 Go) | Tout le calcul : détection d'étoiles, centroïdes, calibration, boucle de guidage, ASTAP, modèle de rotation de champ, pilotage série OnStepX | Il porte la charge. |
| **Téléphone** | Affichage : la page HTML et le JavaScript tournent dans le navigateur, les courbes sont tracées côté client | Le Pi ne génère rien de dynamique. |

### Chemin réseau (en place le 24/09)

Le téléphone (WiFi `AllskyCam`, 192.168.4.x) ne voit pas le réseau USB.

- **Retenu : redirection de port par le noyau sur allsky (nftables).**
  `192.168.4.1:8080` est redirigé vers `192.168.7.1:8080`, l'API du M8S.
  - Service `minicam-m8s-forward`, fichier `/etc/minicam/m8s-forward.nft`.
  - Aucun Python dans la boucle.
  - Seul le port 8080 est relayé : le reste du trafic WiFi → USB est
    refusé, ce qui garde le SSH du M8S injoignable depuis le WiFi.
- **Pourquoi pas un simple routage :** Android envoie vers les données
  mobiles le trafic destiné à un sous-réseau autre que celui d'un WiFi
  « sans internet ». Avec la redirection, le téléphone ne parle qu'à
  192.168.4.1, qui est sur son propre sous-réseau.
- **IP fixe du M8S côté USB :** `192.168.7.1/24`, configurée avec la
  connexion NetworkManager `m8s-usb-allsky`, hors de la plage DHCP
  d'allsky (.10 à .20). Elle était nécessaire parce que la MAC du gadget
  côté M8S est aléatoire (l'IP DHCP changeait à chaque démarrage).
- Les `Conflicts=` entre `minicam-net-usb` et `minicam-net-wifi` sont
  supprimés. Le WiFi AP et l'USB fonctionnaient déjà ensemble, mais un
  `start minicam-net-wifi` aurait coupé le lien USB.

## 3. Flux d'images

### Capteur : allsky est équipée d'un **IMX477** (et non d'un IMX327)

Vérifié le 24/09 (`/etc/minicam/config.toml`, trames reçues). Modes
utiles, avec la lunette chercheuse de 177 mm :

| Mode minicam | Taille | Pixel | Échelle | Champ | Taille packée |
|---|---|---|---|---|---|
| `1080p` (natif, 12 bits) | 1916×1080 | 1,55 µm | **1,81″/px** | 0,96°×0,54° | 3,1 Mo |
| `2028x1080_bin` (binné 2×2 matériel, 10 bits) | 2028×1080 | 3,1 µm | 3,61″/px | 2,03°×1,08° | 2,7 Mo |
| `2028x1520_bin` (binné, 10 bits) | 2028×1520 | 3,1 µm | 3,61″/px | 2,03°×1,52° | 3,9 Mo |

- **Guidage** : mode natif **`1080p`**, sans binning comme demandé par
  l'utilisateur. À 1,81″/px, les étoiles sont bien échantillonnées. Le
  champ de 0,96°×0,54° est plus petit : le nombre d'étoiles guides reste
  à vérifier sous le ciel.
- **Plate solve** : mode **`2028x1520_bin`** (plus grand champ, et le
  binning ramasse plus de lumière), le temps d'une pose. Il faut ensuite
  repasser en mode guidage. ASTAP n'a pas besoin de la finesse du mode
  natif.

### Les deux capteurs et la plage de focales (24/09)

Le M8S prend la taille de pixel du capteur (IMX477 : 1,55 µm ; IMX327 :
2,9 µm) et le binning dans l'en-tête `X-Binned` de chaque trame.
Correction du 24/09 : `fast_990` est binné sans suffixe `_bin`. Il prend
aussi la taille réelle de la trame (recadrage) et la **focale réglée**
(`/optics`, 177 mm retenus).

**Focale de 177 mm :**

| Capteur / mode | Échelle | Champ |
|---|---|---|
| IMX327 `1080p` / `720p` / `540p` / `480p` | 3,38″/px | 1,80×1,01° / 1,20×0,68° / 0,90×0,51° / 0,60×0,45° |
| IMX477 `2028x1520_bin` / `2028x1080_bin` | 3,61″/px | 2,04×1,53° / 2,04×1,08° |
| IMX477 `1080p` / `720p` / `480p` (natifs) | 1,81″/px | 0,96×0,54° / 0,64×0,36° / 0,43×0,24° |
| IMX477 `1080p_bin` / `720p_bin` / `480p_bin` / `fast_990` | 3,61″/px | 1,93×1,08° / 1,28×0,72° / 0,85×0,48° / 1,34×0,99° |

**Plage de focales utilisable** (aucune valeur figée dans le code) :

- **Plate solve (base D50)** : validé sur un champ de 0,43° (image réelle à
  415 mm). En dessous d'environ 0,3° de hauteur, il y a peu d'étoiles de
  la D50 dans le champ : il faut alors solver dans un mode large, ou
  passer à une base plus profonde (D80, à évaluer). Les très grands champs
  (focales < 50 mm, > 5°) restent possibles avec la D50 ; au-delà de
  10°, envisager la base grand champ d'ASTAP (non installée).
- **Guidage** : une échelle guide jusqu'à environ 4 à 6″/px reste
  exploitable (centroïde ≈ 0,1 px). Plus courte : plus d'étoiles mais
  moins de finesse. Plus longue : l'inverse, et un recadrage réduit vite
  le champ (IMX477 `480p` à 415 mm = 0,10°).

### Transport des trames (mesuré le 24/09)

| Voie | Trame de 2,7 à 4,3 Mo | Commentaire |
|---|---|---|
| WebSocket `/ws/raw` existant | 1,4 à 1,7 s | envoi limité à ≈3,4 Mo/s par la pile WebSocket Python du Pi, et dépaquetage fait sur le Pi |
| HTTP brut (lien USB seul) | — | **20 Mo/s** : le lien n'est pas le facteur limitant |
| **`GET /guide/frame` (nouveau)** | **0,15 s** | RAW packé tel quel, sans dépaquetage, compression ni écriture SD. Le dépaquetage prend 0,16 s sur le M8S (`m8s-ctrl/guidecam.py`) |

- Pendant le guidage, la boucle d'aperçu JPEG d'allsky se met en pause
  (fenêtre de 10 s après chaque trame demandée). Le CPU du Pi passe de
  59 % d'inactivité (aperçu seul) à 80 % (guidage à environ 1,5 trame/s).
- **Remarque** : au repos, cette boucle d'aperçu tourne en permanence à
  15 i/s, même sans personne pour regarder, et consomme environ 40 % du
  CPU du Pi. C'est une piste d'optimisation côté minicam, hors de ce
  projet.

### FITS ou PNG ?

| Critère | FITS | PNG |
|---|---|---|
| Coût CPU côté Pi | nul (en-tête + octets bruts) | élevé (compression deflate d'une image 16 bits de 1080p, plusieurs centaines de ms sur le RPi0) |
| Dynamique | 16 bits linéaires, étoiles faibles conservées | 16 bits possibles, mais souvent 8 bits étirés en pratique |
| ASTAP | format natif, lit les indices d'en-tête | accepté, sans métadonnées |
| Taille sur l'USB | 4,1 Mo (3,1 Mo en brut packé) | environ 2 à 3 Mo |

→ Le **brut transite sur l'USB, et le FITS est construit sur le M8S**. Le
**PNG n'est pas utilisé**. Le JPEG sert uniquement à l'affichage.
`/capture.fits`, qui écrit chaque capture sur la carte SD du Pi, n'est plus
nécessaire : le solve utilisera aussi `/guide/frame`.

## 4. Pilotage OnStepX (LX200 série, `/dev/ttyUSB0`, 9600 bauds)

- La connexion reste ouverte en permanence dans un unique service, avec un
  verrou pour sérialiser les commandes. Une ouverture ou une fermeture du
  port redémarre l'ESP32 de la carte E4.
- Commandes utiles :
  - suivi : `:Te#` / `:Td#`
  - GoTo : `:Sr#` `:Sd#` `:MS#`
  - sync : `:CM#`
  - état : `:GU#`
  - impulsions de guidage : `:Mgn/s/e/w<ms>#`
  - alignement : `:A<n>#` / `:A+#`
  - date, heure et site : `:SC#` `:SL#` `:SG#` `:St#` `:Sg#`
- **Chaque commande est vérifiée par relecture de l'état** (`:GU#`,
  `:GR#`/`:GD#`), jamais en se fiant à l'acquittement seul.

### Points à vérifier dans le code source d'OnStepX

- **Type de monture** : `MOUNT_TYPE` (GEM/FORK/ALTAZM) est normalement
  **fixé à la compilation** dans `Config.h`. Il faut vérifier s'il existe
  un réglage à l'exécution. Sinon, le choix EQ/ALT-AZ fait dans l'interface
  doit **refléter** la configuration du firmware (lue et affichée), et non
  la piloter.
- **Impulsions de guidage `:Mg` en ALT-AZ** : elles s'appliquent en RA/Dec,
  et seulement pendant le suivi. Vérifié dans le code et mesuré, voir §6.
- **Alignement par plate solve** : il faut vérifier que `:A+#` associe la
  position mécanique courante aux coordonnées **cibles** courantes
  (`:Sr`/`:Sd`). Si c'est le cas, on règle la cible sur les coordonnées
  *solvées*, puis on envoie `:A+#`, sans avoir à centrer d'étoile.

## 5. Alignement et recalage par plate solve

1. **Préalable : la date, l'heure et le site.** Le M8S n'a pas de RTC
   fiable, et il n'y a pas d'internet au jardin. Il existe cependant deux
   sources déjà à l'heure (information de l'utilisateur) :
   - **allsky**, mis à l'heure par le smartphone qui s'y connecte → source
     principale. Proposition : `chrony` sur allsky en serveur pour `usb0`,
     et le M8S en client. C'est automatique et quasi gratuit en CPU.
   - **OnStepX**, mis à l'heure par la connexion au SWS → le M8S lit
     `:GC#`/`:GL#` et le site `:Gt#`/`:Gg#` pour vérifier la cohérence des
     deux sources. Un écart est affiché en alerte, et le M8S n'écrit
     jamais l'heure dans OnStepX sans action explicite.
2. **Recalage ponctuel** : capture → solve → sync (`:CM#` avec la cible
   réglée sur les coordonnées solvées). On peut ensuite enchaîner avec un
   « GoTo de centrage » optionnel, qui relance le GoTo et un nouveau solve
   jusqu'à un écart inférieur à N″.
3. **Alignement automatique sur N points (1 à 9)** :
   - OnStepX est mis en mode alignement (`:A<n>#`).
   - Le M8S choisit N positions réparties dans le ciel (azimuts espacés,
     hauteur de 30 à 70°, loin du zénith en ALT-AZ).
   - Pour chaque point : GoTo → pose → solve → `:A+#` avec les coordonnées
     solvées.
   - Aucune étoile n'est visée. Le solve prend moins d'1 s sur le M8S avec
     un indice de position. La durée totale est dominée par les GoTo.
4. **Premier solve sans indice** (monture non initialisée) : on lance un
   solve « aveugle » avec ASTAP (`-r 180`). Il est plus lent, de quelques
   secondes à quelques dizaines de secondes avec la base D50. À mesurer.

## 6. Guidage

### Tronc commun EQ / ALT-AZ

- **Détection multi-étoiles** : centroïdes sub-pixel (fond local, seuil
  SNR), en rejetant les étoiles saturées ou trop proches du bord.
- **Mesure par image** : on ajuste une **similitude robuste** (translation
  + rotation) entre les positions de référence et les positions courantes
  des étoiles. On obtient ainsi une **translation du point de référence**
  et un **angle de rotation**.
- **Calibration** : on envoie des impulsions de guidage sur chaque axe
  jusqu'à un déplacement de 20 à 30 px, puis on revient. On en tire la
  matrice caméra ↔ axes monture (angle et taux de chaque axe), on vérifie
  l'orthogonalité et on mesure le jeu mécanique. C'est le même principe que
  PHD2 et le guideur interne d'Ekos.
- **Correcteur par axe** : hystérésis, agressivité, déplacement minimal et
  impulsion maximale (le modèle éprouvé de PHD2). Un correcteur prédictif
  sur l'erreur périodique (EQ) pourra venir plus tard.
- **Interface** : courbes d'erreur des deux axes, RMS total et par axe,
  vignette de l'étoile, SNR, journal.

### EQ

- La calibration est faite en RA/Dec. Il faut corriger le taux RA par
  `1/cos(Dec)` quand on change de déclinaison, et inverser l'axe Dec après
  un retournement au méridien.
- La rotation mesurée doit être nulle. Si elle ne l'est pas, c'est un
  **diagnostic de mise en station** (dérive en rotation = erreur polaire),
  affiché dans l'interface.

### ALT-AZ et rotation de champ

Les images capturées ne sont **pas** dérotées (choix de l'utilisateur). La
rotation de champ intervient seulement dans l'**algorithme de guidage**.

La vitesse de rotation est donnée par :

```
ω_rot = ω_sid · cos(φ) · cos(Az) / cos(Alt)      (Az compté depuis le nord, ω_sid = 15°/h)
```

Elle diverge près du zénith. Il faut donc une **zone d'exclusion** au-dessus
d'environ 75 à 80° de hauteur.

**Effet 1 — direction des corrections : elle tourne, et il faut la compenser.**

*Correction du 24/09 : une première lecture, trop rapide, avait conclu à un
guidage direct sur les axes. C'est faux.*

- Le code d'OnStepX (`Mount.cpp`, `Mount::poll`, lignes 509 à 518) montre
  qu'en ALTAZM les impulsions `:Mg`/`:MG` sont appliquées **en RA/Dec**
  (angle horaire et déclinaison), puis converties en vitesses Az/Alt. Elles
  **n'agissent que pendant le suivi**.
- **Mesure** (24/09, E4) : 2 s vers N donnent ΔDec = +15,1″ ; 2 s vers W
  donnent ΔRA ≈ 15″ et ΔDec = 0. La vitesse est de 0,5× sidéral.
- La caméra est solidaire du tube, donc des axes Alt/Az. Les directions RA
  et Dec tournent dans l'image de la variation de l'**angle
  parallactique** q, à la vitesse ω_rot.
- La matrice de calibration (caméra ↔ RA/Dec) doit donc être **tournée de
  Δq = q(t) − q(t_calib)**, avec :
  `q = atan2(sin H, tan φ·cos δ − sin δ·cos H)` (H, δ lus sur OnStepX, φ la
  latitude).
  C'est exactement ce que fait PHD2 avec un rotateur. Le gain en RA suit
  aussi `1/cos(δ)`, comme en EQ.
- **Sans cette compensation**, le couplage entre les axes grandit avec le
  temps : Δq = 2,5° après 10 min à 15°/h (4 % de couplage), 15° après 1 h
  (26 %). C'est encore pire près du zénith. La mise à jour ne coûte qu'un
  calcul trigonométrique par image.
- **Conséquence heureuse** : EQ et ALT-AZ utilisent **le même guideur** en
  RA/Dec. L'ALT-AZ y ajoute seulement la rotation de la calibration et le
  traitement de l'effet 2.

**Effet 2 — faux déplacement de l'étoile guide : important, et rapide.**
- L'image tourne autour du point suivi par la monture. Une étoile guide à
  une distance r de ce centre décrit un arc de longueur r·Δθ.
- Exemple à 45° de latitude, plein sud, 45° de hauteur : ω_rot ≈ 15°/h.
  Pour une étoile à 400 px du centre (3,38″/px), on obtient :
  - après **1 min**, environ 1,7 px, soit **6″** ;
  - après **5 min**, environ 8,7 px, soit **30″**.
- Un guideur naïf « corrigerait » ce faux déplacement et ferait dériver la
  cible dans l'instrument principal.
- **Traitement** : un ajustement de **similitude** sur plusieurs étoiles
  (translation + rotation) entre les positions de référence et les
  positions courantes. On corrige uniquement la translation du **centre
  de rotation**, qui est aussi le point fixe de la rotation observée et
  peut donc être estimé automatiquement. L'angle mesuré est comparé au
  modèle ω_rot (heure, site, Alt/Az d'OnStepX) pour valider la mesure.
  Avec une seule étoile visible, on retire la rotation prédite par le
  modèle.

**Autres points :**
- Le jeu mécanique doit être compensé **sur les deux axes** : l'azimut
  change de sens au passage du méridien.
- L'interface affiche la pose maximale conseillée pour l'instrument
  principal, ainsi que l'angle et la vitesse de rotation. C'est une
  information seulement, sans dérotation.

**État de l'art vérifié le 24/09 :**

- **PHD2** ne gère pas nativement l'ALT-AZ. Sa documentation et son forum
  recommandent de recalibrer souvent ou d'utiliser un rotateur (PHD2
  sait alors tourner son modèle de calibration selon l'angle de
  position). Ce conseil vaut pour les montures qui guident en RA/Dec, ce
  qui n'est pas le cas d'OnStepX (voir plus bas). Éléments réutilisables
  (licence BSD) : les algorithmes de correction (hystérésis, filtre
  passe-bas, « resist switch », PPEC), la procédure de calibration et la
  rotation du modèle de calibration par un angle.
- **Ekos (guideur interne SEP MultiStar)** : l'erreur est calculée comme
  la **médiane des déplacements** de nombreuses étoiles de référence. La
  rotation n'est pas modélisée, et la médiane est donc biaisée quand le
  champ tourne. Le principe multi-étoiles est à reprendre, mais en
  ajustant une similitude plutôt qu'une médiane.
- **OnStepX** : en ALTAZM, le guidage se fait en RA/Dec, converti en
  interne vers Az/Alt (`Mount.cpp`, vérifié et mesuré le 24/09).
- **Conclusion** : il n'existe pas de guideur ALT-AZ open source clé en
  main. Les briques existent, et seule la partie « mesure tenant compte de
  la rotation » est spécifique. Elle tient en quelques dizaines de lignes
  (ajustement de similitude, par exemple avec
  `cv2.estimateAffinePartial2D`).

## 7. Interface HTML (page d'allsky, calculs sur le M8S)

Onglets proposés, en complément de l'interface caméra existante :

- **Monture** :
  - type EQ/ALT-AZ, lu depuis OnStepX ;
  - synchronisation de l'heure et du site depuis le téléphone ;
  - état (RA/Dec, Alt/Az, suivi, côté de la monture) ;
  - suivi marche/arrêt, GoTo par coordonnées, parking.
- **Alignement** :
  - « Aligner sur N points (auto) » ;
  - « Recaler ici (solve + sync) » ;
  - « Centrer la cible » ;
  - résultat du dernier solve (coordonnées, écart, durée).
- **Guidage** :
  - réglage de la pose ;
  - boucle d'aperçu, choix automatique ou manuel des étoiles ;
  - **lancer la calibration** avec affichage de sa progression et de son
    résultat (angles, taux, orthogonalité, jeu) ;
  - démarrer ou arrêter le guidage ;
  - courbes et RMS ;
  - en ALT-AZ : angle et vitesse de rotation de champ (information
    seulement), pose maximale conseillée, alerte à l'approche du zénith.
- **Réglages** :
  - focale et pixel de la caméra guide ;
  - paramètres du correcteur ;
  - données de l'instrument principal (focale, capteur) pour le calcul de
    la pose maximale ;
  - options d'ASTAP.

Mise à jour de l'état : **SSE ou WebSocket servi par le M8S**, avec un
petit JSON environ 1 fois par seconde. La vignette JPEG vient elle aussi
du M8S.

## 8. Questions ouvertes

1. **Instrument et caméra principaux** : lesquels ? Faut-il faire du
   dithering entre les poses, comme la MGEN-3 (ce qui suppose de connaître
   le mode de déclenchement) ?
2. **EQ et ALT-AZ** : est-ce la même monture, avec un changement de
   firmware OnStepX (`MOUNT_TYPE` fixé à la compilation) ?

Réglé le 24/09 :
- pas de dérotation des images ;
- heure fournie par allsky et OnStepX ;
- mode recadré sans binning pour le guidage.
