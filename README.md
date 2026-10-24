# Tournois Rocket League · Belgique

Page mise à jour automatiquement chaque jour : tournois Rocket League à venir en Belgique
(et en ligne) trouvés sur [start.gg](https://www.start.gg), plus les actus remontées par des alertes Google.

## Fonctionnement

- `veille_rl.py` interroge start.gg et les flux de `flux.txt`, puis génère la page.
- `.github/workflows/veille.yml` le lance chaque matin et publie la page sur GitHub Pages.
- `data/deja_vus.json` mémorise ce qui a déjà été vu, pour l'étiquette « Nouveau ».
- Sur la page, la case « Inscrit » range un tournoi dans « Mes inscriptions » et la croix masque
  un élément pour de bon. Ces choix sont mémorisés dans le navigateur (localStorage), pas dans le dépôt.
- Filtres 1v1 / 2v2 / 3v3 / Tous (1v1 par défaut) et Cash prize. Le format et le cash prize viennent
  de l'intitulé et des épreuves start.gg (taille des équipes, prize money renseigné) ; sans format
  connu, un tournoi apparaît dans les trois.
- Filtres 1v1 / 2v2 / 3v3 / Tous (1v1 par défaut) et Cash prize, d'après l'intitulé du tournoi :
  sans format indiqué, un tournoi apparaît dans les trois ; « Cash prize » = $, € ou £ dans le nom.

## Réglages

- **Alertes Google** : ajouter les adresses des flux RSS dans `flux.txt`.
- **Token start.gg** : secret `STARTGG_TOKEN` (Settings > Secrets and variables > Actions).
  Il expire après un an : en créer un nouveau et remplacer la valeur du secret.
- **Mots exclus, Belgique seule, durée du « Nouveau »** : en haut de `veille_rl.py`.
- **Lancer à la main** : onglet Actions > « Veille des tournois » > Run workflow.
