# Cartouchier — les touches de son de Bobi.Studio

*[English version](README.en.md)*

Les touches de son d'une production en direct. Sous-module de
[bobi.studio](https://github.com/bob-integration/bobistudio), monté sur `plugins/cartouchier/`.

## Ce que ça fait

Une **grille** de touches rangées en pages, ou une **playlist** avec un mode **A/B**. Le son sort
par des **lecteurs** : chacun est une sortie audio stéréo sur le bus MXL (`<hôte>_player_<n>`,
48 kHz).

C'est un plugin à part, et non une fonction de la console audio : un cartouchier peut alimenter
plusieurs consoles. Il est **partagé** par tous ceux qui l'ouvrent — mêmes pages, mêmes touches.
Pour séparer deux positions, on déploie deux cartouchiers.

Tout se fait au doigt comme à la souris.

## Onglets, zones et lecteurs

Un **onglet** regroupe des **zones**, et une zone porte une grille ou une playlist. Le nombre de
lecteurs est un réglage (`players`, 2 par défaut). Un point s'allume sur l'onglet dont un lecteur
joue, même caché.

## Sortie audio

Tous les lecteurs écrivent sur la grille du bus et sont alignés à l'échantillon. Un lecteur à
l'arrêt écrit du silence, sa sortie ne disparaît donc jamais entre deux cartouches. Le conteneur
publie `write_late_max_ms` et son état temps réel sur `:8080`.

## Niveaux EBU R128

Loudness intégrée, crête échantillon et forme d'onde sont mesurées au chargement de chaque média,
puis gardées en cache.

## Montage non destructif

In, Out, fondus, coupes, assemblage, enveloppe de volume et gain sont des **réglages de la
touche** : le fichier n'est jamais touché, et le même son sert plusieurs touches avec des bornes
différentes. « Enregistrer comme nouveau média » produit un fichier monté, l'original restant
intact.

## Écoute

Un flux `GET /listen.pcm` en L16 48 kHz stéréo permet d'écouter le cartouchier, dans la page ou
dans un lecteur tiers.

## Lien public

La page peut s'exposer en lecture seule sur un lien public, un onglet par lien. Créé avec
« autoriser à jouer », le lien permet aussi de lancer et d'arrêter la lecture, et rien d'autre.

## Sauvegardes

Grilles, playlists et pages se rangent dans la bibliothèque commune de l'installation, par genre.
Un rappel ne coupe pas ce qui joue.

## Macros et contrôle externe

Trente et une actions sont publiées aux macros et aux déclencheurs — tirer une touche, arrêter,
changer de page ou d'onglet, A/B, auto, rappeler une grille — plus un `param_tree` pour ce qui se
règle en continu.

## Ressources

Un cœur, 1 Gio déclaré, épinglé. Relevé le 2026-10-30 (r620-2, deux onglets, une zone, un lecteur,
sept cartouches) : 60 à 62 Mio.

## Licence

GNU GPL v3 ou ultérieure — voir `LICENSE`.
