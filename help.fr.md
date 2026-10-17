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

Mode Édition : toucher une touche (ou un emplacement vide) pour la modifier ; ← → la déplacent. Les
médias viennent de la **base des médias** (bibliothèque commune ; un média absent de ce nœud est
d'abord rapatrié, copie vérifiée) ou d'un **fichier envoyé depuis le poste** : bouton « Envoyer un
fichier… », ou un fichier du bureau déposé sur une touche — la touche est affectée à la fin de
l'envoi.

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

Les valeurs sont un instantané : un consommateur qui interroge à quelques hertz interpole entre
deux lectures tant que `state` vaut `playing` ou `fading`, jamais en `paused`. Un champ inconnu
doit être ignoré.

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
