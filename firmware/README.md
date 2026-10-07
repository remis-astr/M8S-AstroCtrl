# Correctif OnStepX : impulsions de guidage immédiates en ALT-AZ

## Le problème (firmware 10.28w, et version amont au 29/09/2026)

Sur une monture ALTAZM, OnStepX n'applique pas une impulsion de guidage
directement au moteur. Il l'ajoute au **calcul des vitesses de suivi**
(`Mount::poll()`, Mount.cpp), qui ne tourne **qu'une fois par seconde**
(tâche `MtTrack`). Au début et à la fin d'une impulsion, `Guide.cpp`
appelle `mount.update()`, et en ALTAZM cette fonction n'ajoute pas la
vitesse de guidage.

Conséquence : l'effet d'une impulsion est un **multiple d'une seconde**.
- Une impulsion de 200 ms produit soit rien, soit une seconde entière
  (7,5″ à 0,5×).
- Cet effet arrive jusqu'à 1 s en retard.

En simulation, le RMS de guidage double à cause de ça :

| Firmware | RMS AD | RMS Dec |
|---|---|---|
| Actuel | 1,8″ | 2,8″ |
| Corrigé | 1,0″ | 1,6″ |

## Le correctif (`onstepx-altaz-pulse-guide.patch`)

- La partie « calcul des vitesses » de `Mount::poll()` devient
  `Mount::updateTrackingRates()`. `poll()` l'appelle toujours une fois par
  seconde.
- Au début et à la fin de chaque impulsion, `Guide.cpp` appelle
  `mount.pulseGuideChanged()`. En ALTAZM/ALTALT, cette fonction recalcule
  les vitesses immédiatement. En équatorial, rien ne change
  (`update()` comme avant).

Il s'applique tel quel sur ta version 10.28w (`d487428`) et sur la version
amont la plus récente. Celle-ci contient aussi un correctif de précision
du calcul des vitesses ALTAZM (commit 21c3834 du 07/09/2026, calcul en
`double`).

## Binaire compilé

`build/OnStepX-10.28w-E4-altaz-pulse-guide.bin` (non versionné) a été
compilé le 29/09 à partir de :
- ta 10.28w ;
- ton `Config.h` et ton `Plugins.config.h` (SWS) ;
- ce correctif.

Carte `esp32:esp32:esp32:PartitionScheme=huge_app,FlashFreq=80,CPUFreq=240`,
cœur 3.3.11 : 1 331 860 octets (42 %).

**Flashé le 07/10/2026** depuis le M8S (esptool 4.8.1 dans `/opt/esptool`,
lancé par `PYTHONPATH=/opt/esptool python3 -m esptool`, `m8s-ctrl` arrêté
pendant l'opération, 115 200 bauds car 460 800 corrompt les données).
Application seule écrite à 0x10000 et vérifiée ; NV inchangée (pas/°,
compensation du jeu, WiFi). Sauvegarde complète de la flash d'avant :
`/root/e4-backup/e4-full-20261007.bin` sur le M8S. Retour arrière :
`write_flash 0x10000` de la zone 0x10000-0x310000 de cette sauvegarde.

Avant le flash, c'était à toi de décider. Les réglages en NV
(pas/degré, heure, site, modèle d'alignement) ne sont normalement pas
effacés par un flash de l'application seule (`.bin`, pas le `.merged.bin`).

## Prochaine compilation (décidé le 07/10/2026)

Pour toute prochaine modification du firmware, partir de la version amont
la plus récente d'OnStepX (au 07/10 : 4 commits après `d487428`), qui
contient `21c3834` (vitesses de suivi ALTAZM calculées en `double`), puis
réappliquer `onstepx-altaz-pulse-guide.patch`, ton `Config.h` et ton
`Plugins.config.h`. Ce correctif amont a un effet négligeable sur notre
guidage actuel (≈ 0,01″/min) : il ne justifie pas un flash à lui seul.
