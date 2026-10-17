# Cartouchier

Des touches sonores pour le direct : une **grille** de touches à **pages**, ou une **playlist**
avec un **mode A/B**. Le son sort par des **lecteurs** : un flux MXL audio stéréo chacun
(`<hôte>_player_<n>`, 2 par défaut, réglable au déploiement). C'est un plugin à part, pas un morceau
de la console audio : la gestion des médias reste hors du fil temps réel de la console, et un
cartouchier peut alimenter plusieurs consoles — ou aucune. Un cartouchier est **commun** à tous ceux
qui l'ouvrent ; pour séparer deux postes, on déploie deux cartouchiers.

## Lecteurs

| État | Sens |
|---|---|
| `idle` | libre — le flux porte du silence |
| `cued` | un son est chargé, prêt (playlist A/B) |
| `playing` | en lecture |
| `paused` | arrêté en cours de son, reprend où il était |
| `fading` | fondu d'arrêt en cours |

Une touche lancée prend un lecteur **libre**, ou celui qui lui est **affecté**. Aucun lecteur
libre : la touche est refusée, et le dit. Toute coupure, pause ou remplacement est adoucie sur
5 ms : jamais de clic.

## Touches

Chaque touche porte : média, titre, gain (dB), **groupe d'exclusivité** (lancer une touche arrête
les autres de son groupe, avec leur fondu), **boucle**, **enchaînement** (à la fin, une autre touche
part — sur le même lecteur, sans trou, sauf si elle a son propre lecteur), **fondu d'arrêt** (ms),
**deuxième appui** en cours de lecture (arrêt avec le fondu, pause / reprise, ou relance depuis le
début), marqueurs **intro** et **fin** (ms depuis le début), lecteur affecté, couleur. Appuyer
pendant le fondu coupe le son.

Mode Édition : le **gestionnaire de médias** s'affiche à côté de la grille (en dessous sur un écran
étroit). Pour affecter un son : le **glisser** sur une touche (à la souris ; au doigt, appui long
sur le média puis glisser), ou **toucher le média** (il reste en surbrillance) **puis la touche**.
C'est immédiat et enregistré ; le titre reprend le nom du média. Sur une touche occupée, seul le son
change, les réglages restent ; un bandeau propose **Annuler** quelques secondes. Un média absent de
ce nœud est affecté tout de suite puis copié sur le nœud (copie vérifiée). Toucher une touche sans
média sélectionné l'ouvre dans l'éditeur ; ← → la déplacent. Un **fichier du poste** se dépose aussi
sur une touche (ou bouton « Envoyer un fichier… ») : la touche est affectée à la fin de SON envoi.

### Défauts des touches

Fondu d'arrêt, deuxième appui, boucle, groupe d'exclusivité et lecteur affecté se règlent **une
fois pour tout le cartouchier** (Réglages → Défauts des touches), et au besoin **par page**. Une
touche **hérite** de sa page, sinon du cartouchier, sauf pour ce qu'elle **personnalise** ;
l'éditeur affiche « Défaut (valeur) » ou la valeur propre, avec « Revenir au défaut ». Changer un
défaut s'applique à toutes les touches qui en héritent ; le fondu d'arrêt même à un son déjà lancé,
les autres réglages au lancement suivant. Un élément de playlist hérite du fondu d'arrêt du
cartouchier. Un cartouchier d'avant 0.1.2 est migré : une valeur égale à l'ancien défaut redevient
héritée, une valeur différente reste personnalisée — rien ne change à l'écoute.

### Nombre de lecteurs

Réglages → Lecteurs : chaque lecteur est une sortie MXL. Changer leur nombre **redéploie** le
cartouchier (ce qui joue s'arrête, les câbles des lecteurs supprimés tombent) : confirmation dans la
page, droit de déployer requis.

## Playlist et A/B

**GO** met à l'antenne l'élément *prêt* et fait disparaître ce qui était à l'antenne avec son
propre fondu. En **A/B**, deux lecteurs alternent : A joue, B tient l'élément suivant prêt ; au GO,
ils échangent. **Suivant / Précédent** déplacent l'élément prêt sans toucher à l'antenne.
L'**enchaînement auto** fait se suivre les éléments seuls.

## Niveaux — EBU R128

Chaque média est mesuré **une fois, au chargement** (sonie intégrée UIT-R BS.1770-4 avec ses
portes ; crête échantillon), et le résultat est mis en cache avec le son décodé. Il est amené à la
**cible** commune du cartouchier (−23 LUFS par défaut), puis le gain propre de la touche s'ajoute.
Une amplification est plafonnée par le gain maximal (relever un enregistrement faible relève son
bruit) et par le plafond de crête (une amplification ne doit pas écrêter) ; une atténuation ne l'est
jamais. Changer la cible s'applique aussitôt, y compris à ce qui joue.

Pourquoi mesuré dans le plugin et non par `ebur128` d'ffmpeg : le son est décodé de toute façon, la
mesure porte sur les échantillons déjà en main (≈ 180 fois le temps réel sur un cœur), et le même
code est vérifié hors ligne sur un signal de sonie connue. Vérifié contre `ffmpeg ebur128`.

## Affichage

Par lecteur : titre, **décompte** au dixième (couleur d'alerte dans les dernières secondes — seuil
dans les Réglages), écoulé / durée, **forme d'onde** avec la tête de lecture et les marqueurs intro /
fin, et les décomptes **intro** et **fin** tant qu'ils sont devant. Dans la grille, une touche en
lecture affiche son propre temps restant et sa barre d'avancement.

## Publication par flux — `GET /flows` (contrat générique)

Un producteur qui lit un contenu minuté publie, **pour chaque flux MXL qu'il produit**, ce qui y
joue. Une console (ou tout consommateur) qui sait quel flux alimente une tranche — et demande à
l'orchestrateur qui le produit — lit ce point d'accès et affiche titre et décompte sur la tranche.
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
| `elapsed_ms`, `remaining_ms`, `duration_ms` | position, temps restant, durée du son |
| `intro_ms`, `outro_ms` | marqueurs, en ms depuis le début du son ; `0` = aucun |
| `intro_remaining_ms`, `outro_remaining_ms` | facultatifs : temps jusqu'au marqueur tant qu'il est devant, sinon `null` |
| `alert_s` | facultatif : seuil (s) sous lequel le décompte passe en couleur d'alerte (réglage du producteur) |

Les valeurs sont un instantané : un consommateur qui interroge à quelques hertz interpole entre
deux lectures tant que `state` vaut `playing` ou `fading`, jamais en `paused`. Un champ inconnu
doit être ignoré.

Le producteur le DÉCLARE dans son manifeste : `control.flow_status: {"endpoint": "/flows", "panel": true}`
(l'endpoint figure dans `read_endpoints` ; `panel` : son interface peut être montée dans le panneau d'un
consommateur, le panneau cartouchier de la console). L'orchestrateur relie flux et producteur par les
noms de `wiring.produces`, lit l'endpoint une fois par producteur (cache ≈ 0,5 s) et sert à la console
`GET /api/console-zones/<vmid>/sources`.

## Sauvegardes : enregistrer, rappeler, exporter, importer

Bouton **Sauvegardes** (barre du haut) : le panneau suit le mode affiché.

- **Grille** : toute la grille (taille, pages, touches, défauts de page). **Playlist** : ses éléments et ses
  options (A/B, enchaînement, boucle). **Page** : la page affichée seule — elle se rappelle *ici* (remplace la page
  affichée) ou *comme nouvelle page*, dans ce cartouchier ou un autre. Chaque touche garde sa ligne et sa colonne ;
  celle qui ne tient pas dans la grille prend le premier emplacement libre (le rapport le dit).
- **Bibliothèque commune** à toute l'installation : une sauvegarde faite ici se rappelle depuis n'importe quel
  cartouchier. Liste avec date et auteur ; **Exporter** donne un fichier JSON (une sauvegarde, ou tout un genre)
  qu'**Importer** relit sur une autre installation. Un nom est unique par genre (« Matinale » peut être à la fois
  une grille et une playlist).
- **Rappel sans coupure** : ce qui joue continue jusqu'au bout ou jusqu'à son arrêt ; seules les touches (ou la
  playlist) changent. Une confirmation s'affiche dans la page avant tout remplacement.
- **Médias** : désignés par leur identité dans la base. À l'import, un média inconnu est signalé ; au rappel, un son
  absent du nœud est listé et la touche reste, marquée « manquant ».
- **Droits** : lister et exporter avec la seule connexion ; enregistrer, supprimer, importer et rappeler avec le
  droit d'exploiter les plugins (`plugins.operate`) — les mêmes que les préréglages.
- **Macros** : « Rappeler une grille », « Rappeler une playlist », « Rappeler une page dans une page » (page cible au
  choix, vide = page affichée, ou ajout d'une page).

## Macros et pilotage externe

Actions : appuyer sur / arrêter une touche (liste vivante des touches), tout arrêter (fondus) / tout
couper, aller à une page, page suivante / précédente, playlist GO / suivant / précédent / préparer un
élément, A/B oui / non, enchaînement auto oui / non, lecteur lancer / pause / arrêter, mode grille ou
playlist. États lisibles : lecteurs (état, temps restant, titre), titre et temps restant de ce qui
joue, élément de playlist à l'antenne et prêt, page affichée. Les réglages de niveau (normalisation,
cible, gain maximal, seuil d'alerte) sont dans l'arbre de paramètres. Les surfaces matérielles et
Ember+ passent par ces actions et ces états, comme pour tout plugin.

## Métriques (`:8080`)

`blocks_per_s` (1000 = le fil audio tient la cadence), `render_ms_max` (pire rendu de la dernière
seconde), `late_blocks` et `reanchors` (le fil a pris plus de 200 ms de retard et s'est ré-ancré sur
la grille TAI — doit rester à 0), `write_errors`, `media_ready` / `media_pending` /
`media_missing`.
