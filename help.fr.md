# Cartouchier

Des touches sonores pour le direct. Deux façons de jouer : une **grille** de touches rangées en
**pages**, ou une **playlist** avec un **mode A/B**. Le son sort par des **lecteurs** : chacun est
une sortie audio stéréo sur le bus MXL (`<hôte>_player_<n>`), que l'on câble vers une console.

Le cartouchier est un plugin à part, pas un morceau de la console audio. La gestion des médias reste
hors du fil temps réel de la console, et un cartouchier peut alimenter plusieurs consoles, ou aucune.
Il est **commun** à tous ceux qui l'ouvrent : mêmes pages, mêmes touches. Pour séparer deux postes,
on déploie deux cartouchiers.

Tout se fait au doigt comme à la souris : les boutons font au moins 44 px, et chaque geste de
glisser a un équivalent en deux touchers.

## L'écran

La **barre du haut** porte, de gauche à droite :

- **Grille** / **Playlist** : le mode affiché ;
- les **onglets de pages** (en mode Grille) : toucher un onglet affiche la page ;
- **Édition** : l'interrupteur qui sépare le jeu (toucher une touche la lance) de la préparation
  (toucher une touche l'ouvre) ;
- **Sauvegardes**, **Réglages** ;
- **🎧 Écoute**, avec la source écoutée ;
- **Tout arrêter** : arrête tous les lecteurs, chacun avec son fondu.

Sous la barre, une **carte par lecteur** ; puis la grille ou la playlist.

Sur un lien public, la page est en lecture seule : on voit les lecteurs et les touches, on ne lance
rien, et l'écoute n'est pas disponible.

## Les lecteurs

Chaque carte de lecteur montre : le numéro (L1, L2…), l'état, le titre, le **décompte** au dixième
de seconde, le temps écoulé sur la durée, la **forme d'onde** avec la tête de lecture et les
marqueurs intro (orange) et fin (rouge). Tant qu'ils sont devant, les décomptes **INTRO** et **FIN**
s'affichent aussi. Dans les dernières secondes, le décompte passe en couleur d'alerte (seuil dans
Réglages → Niveaux, « Alerte du décompte (s) » ; un son en boucle n'alerte pas).

Trois boutons par carte : ▶ **Lancer** (ou reprendre), ❚❚ **Pause** / reprise, ■ **Arrêter** (avec
le fondu).

| État affiché | Sens |
|---|---|
| libre | rien à jouer : la sortie porte du silence |
| prêt | un son est chargé et attend (playlist A/B) |
| lecture | en lecture |
| pause | arrêté en cours de son, reprend où il était |
| fondu | fondu d'arrêt en cours |

Un lecteur libre écrit du silence : la tranche de console qu'il alimente ne perd jamais sa source.
Toute coupure, pause ou remplacement est adoucie sur 5 ms : jamais de clic.

### Nombre de lecteurs

Réglages → **Lecteurs (sorties MXL)**. Deux par défaut, huit au plus. Chaque lecteur est une sortie
MXL, donc changer leur nombre change les sorties du conteneur et **redéploie le cartouchier** :

- ce qui joue s'arrête ;
- en réduisant, les câbles des lecteurs supprimés tombent ;
- la page demande confirmation (« Redéployer maintenant ») en disant combien de lecteurs jouent ;
- il faut le droit de déployer. Sans lui, le nombre s'affiche en lecture seule.

Le contenu du cartouchier (pages, touches, playlist, réglages) survit au redéploiement.

## Jouer en grille

Hors Édition, **toucher une touche la lance**. Elle prend un lecteur libre, ou celui qui lui est
affecté. Sans lecteur libre, la touche est refusée et le message le dit (« Aucun lecteur libre »).
Une touche qui a un lecteur affecté le prend même s'il joue : le son en cours est remplacé.

Une touche en lecture affiche son temps restant et une barre d'avancement. Elle passe en couleur
d'alerte en fin de son, comme le lecteur. Sous le titre, la durée et de petits repères : ⟲ boucle,
→ enchaînement, le nom du groupe d'exclusivité, L*n* pour un lecteur affecté. Un son pas encore
utilisable l'écrit sur la touche : « en attente », « décodage », « absent du nœud », « illisible ».

La grille va jusqu'à 12 × 12 touches, sur 64 pages au plus.

## Ce que fait une touche

- **Boucle** : le son repart au début jusqu'à ce qu'on l'arrête.
- **Enchaîner sur** : à la fin, une autre touche part, sur le même lecteur et sans trou, sauf si
  elle a son propre lecteur affecté.
- **Groupe d'exclusivité** : lancer une touche arrête les autres touches du même groupe, chacune
  avec son fondu. Vide = aucun groupe.
- **Fondu d'arrêt (ms)** : la durée du fondu quand on arrête la touche. 0 = arrêt net (toujours
  adouci sur 5 ms).
- **Deuxième appui**, quand on retouche une touche qui joue :
    - **Arrêt (avec le fondu)** ;
    - **Pause / reprise** ;
    - **Relance depuis le début**.
- Toucher une touche **pendant son fondu** la coupe tout de suite. Toucher une touche **en pause**
  la reprend.
- **Fin de l'intro** et **Début de la fin** (ms depuis le début) : les marqueurs, avec leur décompte.
- **Lecteur affecté** : « Lecteur libre » ou un lecteur précis.
- **Gain de la touche (dB)** : s'ajoute à la normalisation.
- **Couleur** : rouge, orange, jaune, vert, bleu, violet, gris, ou aucune.

## Défauts du cartouchier et de la page

Cinq réglages se règlent **une fois pour tout le cartouchier** : fondu d'arrêt, deuxième appui,
boucle, groupe d'exclusivité et lecteur affecté (Réglages → **Défauts des touches**). La même section
porte l'enchaînement automatique de la playlist.

Une **page** peut s'en écarter : Réglages → **Défauts de la page « … »** (la page affichée). Chaque
champ y indique « Comme le cartouchier (valeur) » tant qu'on ne le personnalise pas.

L'héritage se lit de bas en haut :

- une touche prend **sa propre valeur** si elle en a une ;
- sinon le **défaut de sa page** ;
- sinon le **défaut du cartouchier**.

Dans l'éditeur de touche, un champ hérité affiche « Défaut de la page (valeur) » ou « Défaut du
cartouchier (valeur) », avec **Personnaliser**. Un champ personnalisé propose **Revenir au défaut**.

Changer un défaut s'applique à toutes les touches qui en héritent. Le fondu d'arrêt s'applique même
à un son déjà lancé ; boucle, groupe, deuxième appui et lecteur s'appliquent au lancement suivant.
Un élément de playlist hérite du fondu d'arrêt du cartouchier.

Un cartouchier d'avant 0.1.2 est migré sans rien changer à l'écoute : une valeur égale à l'ancien
défaut devient héritée, une valeur différente reste personnalisée.

## Playlist et mode A/B

En mode **Playlist**, la liste est numérotée. L'élément à l'antenne porte **ANTENNE**, l'élément
suivant porte **PRÊT**.

- **GO** met à l'antenne l'élément prêt et fait disparaître ce qui était à l'antenne, avec son
  propre fondu.
- ◀ **Précédent** / ▶ **Suivant** déplacent l'élément prêt sans toucher à l'antenne.
- Toucher une ligne la rend prête.
- **A/B** : deux lecteurs alternent. A joue, B tient l'élément suivant chargé ; au GO, ils échangent.
  Il faut au moins deux lecteurs.
- **Enchaînement auto** : les éléments se suivent seuls.
- **Boucler la liste** : après le dernier, on repart au premier.

En Édition, chaque ligne gagne ses boutons : ↑ **Monter**, ↓ **Descendre**, ✎ **Modifier**,
🎧 **Pré-écouter**, ✂ **Éditer le clip…**, ✕ **Supprimer**. Un élément porte son titre, son gain, son
fondu d'arrêt, ses marqueurs intro / fin et sa boucle. La playlist compte 500 éléments au plus.

## Préparer : le mode Édition

Activez **Édition**. Toucher une touche ouvre alors son éditeur (une case vide crée une **Nouvelle
touche**). Les boutons ← et → de l'éditeur déplacent la touche d'une case. **Supprimer** demande un
second appui. Les modifications de l'éditeur partent avec **Enregistrer**.

### Affecter un son

En Édition, le panneau **Médias** s'affiche à côté de la grille ou de la playlist (en dessous sur un
écran étroit). C'est la base des médias de l'installation. Hors Édition, il disparaît.

Trois gestes, au choix :

- **À la souris** : glisser un média du panneau sur une touche, ou entre deux lignes de la playlist.
- **Au doigt** : appui long sur le média (environ un tiers de seconde), puis glisser sans lever le
  doigt et lâcher sur la touche. Un doigt qui bouge tout de suite fait défiler la liste au lieu de
  saisir le média.
- **Toucher puis toucher** : toucher le média (il reste sélectionné, le panneau le rappelle), puis
  toucher la touche ou la ligne de playlist. **Désélectionner** annule le choix.

Dans la playlist, le son est inséré à la position touchée ; « Déposer ici pour ajouter à la fin »
l'ajoute en dernier. À la souris, une sélection de plusieurs médias s'insère d'un coup.

L'affectation est **immédiate et enregistrée** : pas de bouton Affecter ni Enregistrer. Le titre
reprend le nom du média. Sur une touche déjà occupée, seul le son change ; la touche garde ses autres
réglages, mais perd son montage (In / Out et coupes de l'ancien son ne veulent rien dire sur le
nouveau).

Un bandeau confirme le geste et propose **Annuler** pendant 8 secondes : la touche retrouve son
ancien son, ou les éléments ajoutés à la playlist sont retirés.

Un média de la base absent de ce nœud est affecté tout de suite, puis copié sur le nœud (copie
vérifiée). La touche affiche « absent du nœud » jusqu'à la fin de la copie, puis le son est décodé.

L'ancien chemin reste disponible : dans l'éditeur de touche, **Base des médias…**.

### Déposer un fichier

Un **fichier du poste** se dépose directement sur une touche ou une case vide, à la souris. Dans
l'éditeur de touche, **Envoyer un fichier…** fait la même chose (pratique sur une tablette). Le fichier entre
dans la base des médias, et la touche est affectée à la fin de **son** envoi. Un autre envoi fait
entre-temps n'atterrit pas sur la mauvaise touche.

### Grille et pages

Réglages → **Grille et pages** :

- **Colonnes** et **Rangées** (1 à 12) ;
- **Nom de la page affichée** ; une page sans nom s'appelle « Page N », d'après son numéro de
  création, qui ne change pas quand on la déplace ;
- **Ajouter une page**, **← Déplacer la page**, **Déplacer la page →**, **Supprimer la page** (second
  appui pour confirmer ; la dernière page ne se supprime pas) ;
- **Enregistrer la grille**.

Réduire la grille ne supprime pas les touches qui n'y tiennent plus : un message les compte, et elles
reviennent quand on agrandit.

## Niveaux — EBU R128

Chaque son est mesuré **une fois, au chargement** : sonie intégrée selon UIT-R BS.1770-4 avec ses
portes, et crête échantillon. Il est amené à la **cible** commune du cartouchier, puis le gain de la
touche s'ajoute.

Réglages → **Niveaux (EBU R128)** :

- **Normaliser au chargement** ;
- **Cible (LUFS)** : −23 par défaut ;
- **Gain maximal (dB)** : plafonne une amplification, car relever un enregistrement faible relève
  aussi son bruit ;
- **Plafond de crête (dBFS)** : une amplification ne doit pas écrêter ;
- **Alerte du décompte (s)**.

Une atténuation n'est jamais plafonnée. Changer la cible s'applique aussitôt, y compris à ce qui
joue. L'éditeur de touche affiche la mesure : « Mesuré … LUFS, crête … dBFS → normalisation … dB »,
et dit si la normalisation a été limitée par le gain maximal ou par le plafond de crête.

La mesure est faite dans le plugin, sur les échantillons déjà décodés (environ 180 fois le temps réel
sur un cœur), avec un code vérifié hors ligne sur un signal de sonie connue et comparé à
`ffmpeg ebur128`.

## Écouter

Le bouton **🎧 Écoute** de la barre joue le son dans le navigateur. Une seule source à la fois, au
choix dans la liste à côté :

- **Lecteur N** : ce qui part réellement vers la console ;
- **Pré-écoute** : un lecteur propre au cartouchier, **hors antenne**. Il n'écrit sur aucune sortie
  MXL : seuls les navigateurs qui écoutent la pré-écoute l'entendent.

Pour pré-écouter un son avant de le lancer :

- le 🎧 d'une touche, en Édition ;
- **🎧 Pré-écouter** dans l'éditeur de touche ou d'élément ;
- le 🎧 d'une ligne de playlist, en Édition ;
- les boutons 🎧 de l'éditeur de clip.

Si l'écoute était sur un lecteur, elle bascule sur la pré-écoute ; si elle était coupée, elle
démarre. La barre affiche « Pré-écoute : *titre* » et un bouton **■ Arrêter la pré-écoute**. Une
nouvelle pré-écoute remplace la précédente. Elle s'arrête d'elle-même quand le dernier navigateur
qui l'écoute s'en va.

Le choix de la source est retenu par navigateur. Huit écoutes au plus sont ouvertes en même temps sur
un cartouchier.

## Éditeur de clip

**✂ Éditer le clip…**, depuis l'éditeur de touche, l'éditeur d'élément ou une ligne de playlist,
ouvre l'éditeur presque en plein écran.

L'édition est **non destructive** : le fichier n'est jamais modifié. In, Out, coupes, fondus,
enveloppe et gain sont des réglages de la touche (sa liste de montage), appliqués à la lecture. Le
même son peut donc servir à plusieurs touches avec des découpes différentes. Le décompte, la durée,
les marqueurs et ce que voit la console comptent la durée montée.

### Se déplacer

- **Montage** / **Source** : la vue du clip monté (fondus, enveloppe, marqueurs), ou le fichier du son
  choisi (In, Out, coupes).
- Toucher la forme d'onde place le **curseur**.
- Un doigt fait défiler ; deux doigts pincent pour zoomer. À la souris, la molette zoome.
- Boutons **−** / **+** (zoom), **Tout**, ◀ **Vers le début**, ▶ **Vers la fin**.

### In, Out et coupes (vue Source)

- **In au curseur**, **Out au curseur** ; ou les champs « In (ms dans le fichier) » et « Out (ms dans
  le fichier, 0 = fin) ».
- **Début de sélection** et **Fin de sélection** marquent un passage au curseur.
- **Couper la sélection** retire le passage. Un micro-fondu de 10 ms adoucit le raccord.
- Chaque coupe est listée avec **🎧 Écouter le raccord** et ✕ pour la retirer.

### Fondus, enveloppe, marqueurs (vue Montage)

- **Fondu d'entrée jusqu'au curseur**, **Fondu de sortie depuis le curseur** ; ou les champs en ms.
- Forme de chaque fondu : **Linéaire**, **Puissance constante**, **En S**, **Exponentielle**.
- **Point de volume au curseur** ajoute un point à l'enveloppe. Un point se glisse au doigt ou à la
  souris (temps et niveau), se règle aussi en ms et en dB dans la liste, et **Supprimer le point**
  le retire.
- **Fin de l'intro au curseur**, **Début de la fin au curseur**.
- **Gain de la touche (dB)**.

### Assembler plusieurs sons

- **+ Ajouter un son à la suite**, puis choisir le son dans le panneau Médias.
- Les sons sont numérotés dans la barre ; toucher un numéro le choisit.
- Pour chaque son après le premier : **Fondu enchaîné avec le son précédent (ms)** et sa forme.
- **Placer plus tôt**, **Placer plus tard**, **Retirer ce son du montage**.
- La normalisation R128 s'applique à chaque son séparément.

### Écouter dans l'éditeur

La ligne 🎧 joue le **brouillon**, même non enregistré : **Tout le clip**, **La sélection**,
**Autour du curseur**, et en vue Source **Autour de In** et **Autour de Out**. **Avant (s)** et
**Après (s)** règlent la durée écoutée autour du point. ■ arrête.

### Enregistrer

- ↶ / ↷ : annuler ou rétablir une modification.
- **Enregistrer** écrit le montage sur la touche ou l'élément.
- **Abandonner** ferme sans enregistrer, après confirmation dans la page.
- **Enregistrer comme nouveau média…** : le montage est rendu dans un **nouveau fichier** WAV 24 bits
  de la base des médias, à côté du premier son. L'original n'est pas touché. Donnez un nom, puis
  **Créer le média** ; l'avancement s'affiche. Si des échantillons ont écrêté, le message le dit :
  baissez le gain. Une fois le média créé, **Utiliser le nouveau média ici** le met sur la touche, à
  la place du montage. Le nouveau média sert ailleurs : un player, un autre cartouchier.

## Sauvegardes

Le bouton **Sauvegardes** ouvre un panneau qui suit le mode affiché.

- En mode Grille : la **Grille** entière (taille, toutes les pages, leurs touches et leurs défauts
  de page), et la **Page affichée** seule.
- En mode Playlist : la **Playlist** (éléments et options A/B, enchaînement, boucle).

Rappeler une playlist ne touche pas à la grille, et inversement.

Pour chaque genre :

- **Nom** puis **Enregistrer … sous…**. Un nom est unique par genre : « Matinale » peut être à la
  fois une grille et une playlist. Un nom déjà pris demande confirmation avant d'être remplacé.
- La liste montre le nom, la date, l'auteur et un résumé (pages, touches ou éléments).
- **Rappeler**. Pour une page : **Rappeler ici** (remplace la page affichée) ou **Ajouter comme
  nouvelle page**, dans ce cartouchier ou un autre. Chaque touche garde sa ligne et sa colonne ; une
  touche qui ne tient pas dans la grille prend le premier emplacement libre, et le rapport le dit.
- **Exporter** une sauvegarde, ou **Tout exporter** d'un genre, dans un fichier JSON.
- **Importer un fichier…** relit ce fichier, par exemple sur une autre installation. **Remplacer les
  noms déjà pris** décide du sort des doublons ; le rapport compte les sauvegardes importées,
  ignorées et refusées.
- **Supprimer**, après confirmation.

La bibliothèque est **commune à toute l'installation** : une sauvegarde faite ici se rappelle depuis
n'importe quel cartouchier.

Le **rappel ne coupe rien** : ce qui joue continue jusqu'au bout ou jusqu'à son arrêt ; seules les
touches ou la playlist changent. Une confirmation s'affiche dans la page avant tout remplacement.

Les sons sont désignés par leur identité dans la base des médias. À l'import, un son inconnu de la
base est signalé, et la sauvegarde est gardée. Au rappel, un son absent du nœud est listé dans le
rapport ; la touche est gardée et marquée comme manquante.

Droits : lister et exporter avec la seule connexion ; enregistrer, rappeler, importer et supprimer
avec le droit d'exploiter les plugins.

## Dans la console audio

Trois liens avec la console, sans changer de page :

- **Décompte sur la tranche** : une tranche alimentée par un lecteur du cartouchier affiche
  d'elle-même le titre, le décompte et l'intro / fin de ce qui joue. Elle passe en couleur d'alerte
  au même seuil que le cartouchier.
- **Fader start** : ouvrir le fader de la tranche lance ce que le lecteur tient prêt (l'élément prêt
  en A/B) ou reprend un son en pause. Le lien se crée dans la configuration de la tranche (Tranches
  et bus) : **Lier à un lecteur…**, puis choisir le producteur et le lecteur. La tranche porte alors
  la pastille **FS**. Il faut que la console et le cartouchier soient dans un même projet, et le rôle
  éditeur de ce projet. Ce que fait la fermeture du fader se règle par lien :
    - **arrêt** (avec le fondu) ;
    - **pause** ;
    - **rien**.
- **Panneau cartouchier** : le bouton **Cartouchier** de la barre du haut de la console ouvre
  l'interface du cartouchier dans un panneau. Il apparaît dès qu'un cartouchier alimente une prise
  de la console. Réglages de la console → **Cartouchier** permet d'en proposer d'autres.

## Macros et pilotage externe

Actions :

- appuyer sur une touche (lancer, ou son deuxième appui), arrêter une touche ;
- tout arrêter (fondus), tout couper (sans fondu) ;
- aller à une page, page suivante, page précédente ;
- playlist : GO, suivant, précédent, préparer un élément (et le lancer aussitôt) ;
- A/B activé ou désactivé, enchaînement automatique activé ou désactivé ;
- lecteur : lancer / reprendre, pause / reprise, arrêter ;
- mode grille, mode playlist ;
- rappeler une grille, rappeler une playlist, rappeler une page dans une page (page cible au choix,
  ou ajout d'une page) ;
- pré-écouter une touche, arrêter la pré-écoute.

Les listes de touches, de pages, d'éléments et de sauvegardes sont vivantes.

États lisibles : lecteurs en cours ; titre et temps restant de ce qui joue ; mode ; page affichée ;
A/B ; élément à l'antenne (numéro et titre) et élément prêt ; état, temps restant et titre des
lecteurs 1 et 2 ; état et titre de la pré-écoute.

Dans l'arbre de paramètres : les niveaux (normalisation, cible, gain maximal, seuil d'alerte) et les
défauts des touches. Les surfaces matérielles et Ember+ passent par ces actions et ces états, comme
pour tout plugin.

## Pour les intégrateurs

### Publication par flux — `GET /flows` (contrat générique)

Un producteur qui lit un contenu minuté publie, **pour chaque flux MXL qu'il produit**, ce qui y
joue. Une console (ou tout consommateur) qui sait quel flux alimente une tranche, et demande à
l'orchestrateur qui le produit, lit ce point d'accès et affiche titre et décompte sur la tranche.
Rien dans ce contrat n'est propre au cartouchier : le player ou le recorder peuvent publier la même
chose.

`GET /flows` (un `read_endpoint`, donc lisible avec la seule connexion via
`/api/containers/<vmid>/plugin/flows`) rend une entrée par flux produit, **lecteurs libres
compris** (un consommateur doit distinguer « libre » de « non publié ») :

```json
{
  "studio-cart_player_1": {
    "title": "Jingle infos",
    "state": "playing",
    "player": 1,
    "elapsed_ms": 4200,
    "remaining_ms": 7800,
    "duration_ms": 12000,
    "intro_ms": 3000,
    "outro_ms": 9500,
    "intro_remaining_ms": null,
    "outro_remaining_ms": 5300
  }
}
```

| Champ | Sens |
|---|---|
| clé | le nom du flux MXL, exactement celui de `wiring.produces` |
| `title` | ce que lit l'exploitant ; `""` si libre |
| `state` | `idle` · `cued` · `playing` · `paused` · `fading` |
| `player` | numéro du lecteur dans ce producteur (base 1) |
| `elapsed_ms`, `remaining_ms`, `duration_ms` | position, temps restant, durée du son (durée montée si la touche a un montage) |
| `intro_ms`, `outro_ms` | marqueurs, en ms depuis le début du son ; `0` = aucun |
| `intro_remaining_ms`, `outro_remaining_ms` | facultatifs : temps jusqu'au marqueur tant qu'il est devant, sinon `null` |
| `alert_s` | facultatif : seuil (s) sous lequel le décompte passe en couleur d'alerte (réglage du producteur) |

Les valeurs sont un instantané : un consommateur qui interroge à quelques hertz interpole entre
deux lectures tant que `state` vaut `playing` ou `fading`, jamais en `paused`. Un champ inconnu
doit être ignoré.

Le producteur le DÉCLARE dans son manifeste : `control.flow_status: {"endpoint": "/flows", "panel": true}`
(l'endpoint figure dans `read_endpoints` ; `panel` : son interface peut être montée dans le panneau
d'un consommateur, le panneau cartouchier de la console). L'orchestrateur relie flux et producteur
par les noms de `wiring.produces`, lit l'endpoint une fois par producteur (cache ≈ 0,5 s) et sert à
la console `GET /api/console-zones/<vmid>/sources`.

### Écoute — `GET /listen.pcm`

Flux déclaré dans `control.streams` (droit de lecture), relayé tel quel par l'orchestrateur, au même
format que l'écoute de la console : `audio/L16;rate=48000;channels=2`, entiers 16 bits petit-boutiste
entrelacés, par paquets de 10 ms depuis le bord vivant. `?src=pfl` pour la pré-écoute,
`?src=1`…`N` pour un lecteur. Huit clients au plus (`503 too_many_listeners` au-delà). Un client
lent perd son audio le plus ancien ; il ne ralentit jamais le fil audio.

### Autres points d'accès ajoutés depuis 0.1.2

| Point d'accès | Rôle |
|---|---|
| `POST /assign` | affecter un média à une touche (`page`, `slot`, `media`, `title`) ; persisté |
| `POST /defaults` | défauts des touches du cartouchier, ou d'une page avec `page` ; lus dans `/state.defaults` |
| `GET /capture?kind=&page=` | capture d'une grille, d'une playlist ou d'une page pour la bibliothèque |
| `POST /recall` | rappel `{kind, payload, name, page, add}`, sans coupure ; persisté |
| `POST /preview`, `POST /preview/stop` | pré-écoute d'une touche ou d'un élément (`key` ou `item`, `start_ms`, `end_ms`, brouillon `edit` et `gain_db` facultatifs) ; état dans `/state.preview` |
| `GET /wave` | fenêtre de forme d'onde d'un fichier source (`key`, `item` ou `asset`/`file`, `from_ms`, `to_ms`, `points`) |
| `POST /montage/wave` | fenêtre de la timeline montée, avec sa disposition |
| `POST /montage/render`, `GET /montage/job?id=` | « Enregistrer comme nouveau média » : rendu WAV 24 bits en tâche de fond, puis suivi |

La bibliothèque des sauvegardes vit dans l'orchestrateur (préréglages typés, `control.recall`,
genres `grid`, `playlist`, `page`). Le nouveau média porte son identité dans son fichier
d'accompagnement (`origin="cartouchier"`, `derived_from`).

### Réglages de déploiement

- **Lecteurs (flux stéréo)** : 2 par défaut ; redéploie.
- **Durée maximale d'un son (s)** : 1200 par défaut ; un média plus long est tronqué au décodage.
- **Cache des sons décodés (Mo)** : 4096 par défaut. Les sons sont décodés une fois (PCM 48 kHz
  stéréo, environ 23 Mo par minute) sur le volume d'état du conteneur. Au-delà du plafond, les sons
  les plus anciennement utilisés et qu'aucune touche ne référence sont effacés.

### Métriques (`:8080`)

`blocks_per_s` (1000 = le fil audio tient la cadence), `render_ms_max` (pire rendu de la dernière
seconde), `late_blocks` et `reanchors` (le fil a pris plus de 200 ms de retard et s'est ré-ancré sur
la grille TAI ; doit rester à 0), `write_errors`, `media_ready` / `media_pending` /
`media_missing`.
