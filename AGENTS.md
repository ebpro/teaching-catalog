# AGENTS.md - teaching-catalog

## Classification

- Depot d'outillage de corpus : il genere le catalogue des supports d'enseignement de l'organisation. Ce n'est ni un cours, ni un ELO, ni un consommateur du pipeline de rendu Quarto.
- Owner unique par session (ADR-01, ADR-06). Depuis ADR-16, le lieu durable du contexte de session de ce depot est le present fichier.

## Role dans la flotte

- Ce depot est **producteur** du manifest que le depot `homepage` consomme. Un changement de champ, de nom ou de semantics du manifest est un changement pour `homepage` : il se coordonne la-bas, pas en adaptant `homepage` en silence.
- Les valeurs d'epinglage de flotte (versions d'outillage partage, images de rendu, references de workflow) ne vivent pas ici : `teaching-repos/TEACHING-FLEET.md` et le serveur en font foi.

## Ce que le depot produit

- `catalog.py` est le generateur unique : il lit les depots de l'organisation et leurs topics, et ecrit un site statique (`public/`) et les cartes de racine (`roots/`).
- La destination de publication est le chemin `teaching-material/catalog/` du serveur de rendu. Le rsync de ce chemin est fait **avec** suppression, bornee a `catalog/` : il ne doit jamais toucher les autres chemins de `teaching-material/`, qui appartiennent aux publications des cours.
- Un controle cote hote verifie le mode des fichiers poses. La lecon incorporée : un envoi sans preservation des permissions tombe sous l'umask du compte distant, et un chmod local sur un fichier temporaire ne corrige rien.

## Regles de conception

- Le classement d'un depot vient de ses topics `area-*`, selon une priorite (`lecture` avant `notebook` avant `sample` avant les autres) et un repli par prefixe de nom de depot. Une correction de classement se fait donc sur les topics de l'organisation, pas dans ce code. Une correction forcee dans le code est une exception assumee : elle doit rester nommee comme telle et rester minimale.
- Les comptes ne sont pas des constantes. Nombre de cartes, de depots classes, de fiches : tout cela se mesure a chaque generation et ne s'ecrit nulle part comme une valeur attendue.
- Un fait date n'entre ni dans le code, ni dans la zone mince de ce fichier.
- La liste des formations et des filieres se dedupe des topics, pas d'une table interne : une filiere nouvelle doit pouvoir etre enseignee sans modification de ce depot.

## Publication et autorisations

- Le flux de publication se declenche sur **poussee vers la branche par defaut**, sur tache periodique, et sur declenchement manuel. Le job de deploiement n'a **aucune condition d'evenement** : une poussee, meme purement documentaire, reconstruit et **republie** le catalogue en ligne.
- Cette consequence doit etre nommee avant toute demande de poussee ; elle se verifie dans le fichier de workflow, pas dans ce resume.
- Toute poussee : go-ahead humain individuel (ADR-04), portes fraiches avant, double lecture serveur apres, jamais `--force`, `-u`, `--tags`.

## Validation avant "fait"

- Une modification se valide par une generation reelle et la relecture des fichiers produits (`public/`, `roots/`, manifest), pas par la lecture du diff.
- Une publication se prouve cote serveur : signature du corps de page et en-tete de date du fichier servi. Un statut 200 seul ne prouve ni la publication ni la fraicheur.
- Les verdicts de CI se lisent au niveau des **jobs**, jamais au niveau du run.

<!-- ADR-REGISTRY:BEGIN source=Teaching/TEACHING-FLEET.md -->
## Registre ADR

| ADR | Règle | Statut |
|---|---|---|
| ADR-01 | Un owner formel unique par projet. Une règle, pas un constat : tous les owners ne sont pas désignés. | actif |
| ADR-02 | Une fiche de démarrage est du contexte de session, jamais un fait daté. | rescopé par ADR-18 : le lieu durable du contexte d'un projet est son propre `AGENTS.md` versionné ; la distribution par copie n'est plus la voie |
| ADR-03 | Aucun énoncé d'état (commit, poussée, tag, fusion, écriture, suppression) sans relecture côté serveur ou côté dépôt. Une reconstitution qui n'exécute pas l'outil ne prouve rien. | actif |
| ADR-04 | Go-ahead humain explicite, formulé pour cette ressource et cette action, par mutation. Une autorisation dont la charge change est périmée et s'obtient de nouveau. | actif |
| ADR-05 | Le gel de la flotte est décidé et levé par l'humain. Sa valeur courante ne vit pas dans un fichier de doctrine : elle se lit côté serveur et au lieu d'état que nomme `TEACHING-FLEET.md`. | actif ; la valeur que ce registre portait en est retirée par ADR-18 |
| ADR-06 | Un owner unique pour `_quarto-utils` ; les autres flux n'y touchent pas. | actif |
| ADR-07 | Timeout borné sur toute commande, fail-fast sur toute atteinte non vérifiée. Une vérification perdue n'est jamais une vérification réussie. | actif |
| ADR-08 | Une preuve durable a un lieu durable : un ref, un tag, un artefact versionné. Jamais un fichier temporaire, jamais un objet git non atteignable. | actif |
| ADR-09 | Éligibilité scrub : fence `{.bash}`, légende « Capture de l'atelier - commandes et sortie, non exécutées par le rendu : », et honnêteté par document (vérifier `execute:` et `eval:` du document avant toute formulation). | actif |
| ADR-10 | Un `404` ne veut pas dire absent : l'API de contenu et l'arbre git ne couvrent pas le même espace. | actif |
| ADR-11 | Bundle du travail avant toute mutation git. | actif |
| ADR-12 | zsh ne découpe pas les mots : tableaux, ou sortie délimitée et lue proprement. | actif |
| ADR-13 | Le journal du corpus est la source unique de l'état **transverse** ; l'état d'un seul dépôt vit dans l'`AGENTS.md` de ce dépôt. | rescopé par ADR-17 |
| ADR-14 | Un projet ne publie pas son propre verdict. | actif |
| ADR-15 | *Aucun énoncé : cet identifiant n'a jamais été émis.* Il n'est pas remplacé, pour que les références à ADR-16 et ADR-17 restent vraies. | non émis |
| ADR-16 | Une passe de transfert d'état n'est pas une mission de contenu : pas de leçon, pas de TP, pas de relance de CI, pas de changement d'épinglage. Un dépôt par rédacteur ; une reprise commence par une remesure fraîche. | actif |
| ADR-17 | Un `AGENTS.md` de projet porte une zone mince sans fait daté, puis une unique section de registre de session. | modifié par ADR-18 : la borne ne se pose plus par position mais par classe de région, §11 |
| ADR-18 | Trois classes de région, §11 : ce qui est gelé est l'information et ses ancres, pas la phrase ; la zone mince est éditoriale sous retrait nommé ; le plancher dur de la mutation est inchangé. | actif |

## Invariant de vérification des montages

Un montage est l'intersection entre les entrées de `.gitmodules` et les répertoires réellement
présents à la racine du dépôt. Le total des liens git renvoyés par un `git ls-tree` récursif **n'est
pas** un nombre de montages. Ne jamais énoncer un nombre de montages dans un fichier de doctrine :
le mesurer à la demande.
<!-- ADR-REGISTRY:END -->

## Registre de session (zone ledger, hors Validation 5)

Fait date autorise ici uniquement ; la clause 5 de TEACHING-FLEET.md s'arrete a la ligne precedente.

### Identite

- Depot `ebpro/teaching-catalog`, branche par defaut `main`, public, clone sous `teaching-repos/teaching-catalog` cree le 2026-11-07 par la vague de creation des projets manquants (ruling humain : "crée les projets manquants en sous-repertoire frere"). Aucun `AGENTS.md` n'existait dans ce depot avant ce fichier : `git ls-files` a la creation du clone le confirme.
- Aucun sous-module (mesure : `git config --file .gitmodules --get-regexp path` repond vide). 7 fichiers suivis.

### Etat mesure a la creation du clone

- Parite avec la distante verifiee : HEAD local `ad51769131d722c35388245d4a627ad6915a8efb` == `git ls-remote origin refs/heads/main`.
- Contenu de la racine, mesure : `catalog.py` (2208 lignes), `inputs.css`, `tailwind.config.js`, `README.md`, `.gitignore`, `.github/workflows/catalog.yml` (142 lignes), `.github/workflows/cleanup.yml` (47 lignes).
- `catalog.yml` : le job `build` tourne sur l'executeur heberge, le job `deploy` sur le pool auto-helberge de l'organisation (`needs: build`, **sans `if:`**), et depose par rsync vers `teaching-material/` avec un `--delete` borne a `catalog/`.
- `cleanup.yml` : `CF Pages Cleanup`, tache periodique hebdomadaire. Heritage d'une plateforme de publication precedente ; son effet reel sur l'etat actuel n'est **pas verifie** par cette session.
- Le depot ecrit un artefact d'Actions (`Upload catalog site artifact`) que le job de deploiement telecharge : il contribue donc au compteur de stockage de l'organisation. Le chiffre du compteur est un fait de flotte, pas un fait de ce fichier.

### Missions ouvertes

- Instruire `cleanup.yml` : savoir si ce menage cible encore quelque chose de reel, ou s'il est l'heritage d'une plateforme abandonnee. Decision d'owner.
- La vague d'extension du registre emis (ADR-18) vers les depots qui n'ont pas encore de ledger est planifiee par le journal de corpus ; le present depot entre dans la liste declaree par ce meme commit.
- Relecture du contenu editorial de la table ADR par un second regard : la porte de derive prouve la forme, jamais le sens.

### Decisions humaines en attente

- Poussee du present commit : **elle republierait le catalogue en ligne**, le job de deploiement n'etant conditionne par aucun type d'evenement. Le go-ahead doit nommer cette consequence (ADR-04).
- Disposition de `cleanup.yml` si son heritage est confirme.

### Non negociables propres au depot

- Ne jamais etendre le rsync de suppression au-dela de `catalog/` : les autres chemins de `teaching-material/` sont publies par les depots de cours.
- Ne pas corriger un classement dans `catalog.py` quand la correction est un topic : la source du classement est l'etiquetage de l'organisation.
- Ne pas enoncer de compte de cartes ou de depots comme une valeur attendue, nulle part.

### Pointeurs

- `teaching-repos/TEACHING-FLEET.md` : regles de flotte, seule autorite sur les valeurs partagees.
- `Teaching/AGENTS.md` : journal d'audit du corpus, etat transverse date, registres de decisions.
- `homepage/AGENTS.md` : le consommateur du manifest produit ici.
