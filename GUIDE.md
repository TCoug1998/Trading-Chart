# Trading Chart : guide d'installation

Durée : environ 15 minutes, une seule fois. Un ordinateur est plus confortable pour les étapes 1 à 5, le téléphone suffit ensuite.

Résultat : l'app Trading Chart sur ton téléphone avec ta liste de surveillance, le graphique et ton indicateur 200 SMA, plus une alerte push et e-mail à chaque signal ACHAT ou VENTE sur le SPX.

## 1. Créer le dépôt GitHub

1. Crée un compte gratuit sur https://github.com si tu n'en as pas.
2. Bouton **New repository**. Nom : `spx200`. Visibilité : **Public** (nécessaire pour l'hébergement gratuit). Valide avec **Create repository**.

Le code et les cours de bourse seront visibles publiquement. Aucune donnée personnelle n'y figure : ton e-mail, tes mots de passe et ton abonnement push restent dans les secrets GitHub, qui ne sont jamais visibles.

## 2. Déposer les fichiers

1. Décompresse `TradingChart_App.zip` sur ton ordinateur.
2. Dans le dépôt : **Add file**, puis **Upload files**. Glisse **tout le contenu** du dossier décompressé (pas le dossier lui-même). Valide avec **Commit changes**.
3. Vérifie que le dossier `.github/workflows` apparaît dans le dépôt. Sur Mac, les dossiers commençant par un point sont masqués : si `.github` manque, fais **Add file**, **Create new file**, tape le nom `.github/workflows/update.yml`, colle le contenu du fichier `update.yml` et valide.

## 3. Activer l'hébergement

**Settings**, puis **Pages**. Dans **Source**, choisis **GitHub Actions**.

## 4. Configurer l'e-mail (secrets)

**Settings**, **Secrets and variables**, **Actions**, puis **New repository secret** pour chacun :

| Nom | Valeur (exemple Gmail) |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | ton adresse Gmail |
| `SMTP_PASSWORD` | un **mot de passe d'application** Gmail (voir ci-dessous) |
| `ALERT_EMAIL_TO` | l'adresse qui reçoit les alertes |

Mot de passe d'application Gmail : https://myaccount.google.com/apppasswords (la validation en deux étapes doit être active). Crée-en un nommé « SPX200 » et colle les 16 caractères. N'utilise jamais ton mot de passe Gmail principal. Autre messagerie : utilise son serveur SMTP, port 465 ou 587.

## 5. Premier lancement

Onglet **Actions**. Si GitHub le demande, active les workflows. Ouvre **Mise à jour des données**, puis **Run workflow**. Après 2 à 4 minutes, l'app est en ligne à l'adresse :

`https://<ton-identifiant>.github.io/spx200/`

## 6. Notifications push (sur le téléphone)

1. Ouvre l'adresse de l'app sur ton téléphone. Onglet **Alertes**, carte **Clé d'alerte**, bouton **Générer la clé**, puis **Copier la clé**.
2. Sur GitHub (le navigateur du téléphone suffit) : crée le secret `VAPID_PRIVATE_KEY` avec cette clé.
3. Relance **Mise à jour des données** (étape 5) et attends la fin.
4. Installe l'app sur l'écran d'accueil :
   * Android, Chrome : menu ⋮, **Installer l'application** ou **Ajouter à l'écran d'accueil**.
   * iPhone, Safari : bouton Partager, **Sur l'écran d'accueil**. Obligatoire sur iPhone pour recevoir les notifications (iOS 16.4 minimum).
5. Ouvre l'app **depuis son icône**, onglet **Alertes**, bouton **Activer les notifications**, accepte, puis **Copier le code**.
6. Sur GitHub : crée le secret `WEBPUSH_SUBSCRIPTION` avec ce code.

Plusieurs appareils : mets les codes entre crochets, séparés par une virgule : `[code1, code2]`.

## 7. Tester

**Actions**, **Mise à jour des données**, **Run workflow**, coche **Envoyer une alerte de test**. Tu dois recevoir une notification et un e-mail en moins de 5 minutes.

## Fonctionnement

* Mise à jour toutes les heures en semaine, toutes les 6 heures le week-end. Les cours viennent de Yahoo Finance, en différé.
* Signal confirmé : envoyé à la première mise à jour après la clôture US, vers 22:20 heure de Paris. Les tâches planifiées de GitHub peuvent avoir jusqu'à 30 minutes de retard.
* Pré-alerte : envoyée une fois dans la dernière heure de séance si le signal se formerait à la clôture au cours actuel. Ce n'est pas une confirmation.
* Rattrapage : si des mises à jour ont échoué, un signal des 10 dernières séances non encore notifié est envoyé au retour.

## Entretien

* **Changer la liste** : modifie `config/symbols.json` sur GitHub (code Yahoo dans `yahoo`, nombre de décimales dans `dp`). Les symboles introuvables sont listés en bas de l'onglet Liste.
* **Abonnement push expiré** (fréquent sur iPhone après une réinstallation) : l'e-mail d'alerte te le signale. Refais l'étape 6, points 5 et 6.
* **Inactivité** : GitHub suspend les tâches planifiées d'un dépôt public sans activité pendant 60 jours. L'app enregistre un petit fichier chaque mois pour l'éviter. Si GitHub t'envoie malgré tout un e-mail de suspension, réactive le workflow dans l'onglet Actions.
* **Mot de passe ou clé compromis** : remplace le secret concerné sur GitHub. Pour la clé d'alerte, génère-en une nouvelle puis refais l'étape 6.
