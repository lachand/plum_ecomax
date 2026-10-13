# Bienvenue sur la doc Plum EcoMAX

Cette intégration permet de connecter les chaudières **Plum** (via un convertisseur RS-485 vers Ethernet/Wi-Fi, ou le module ecoNET) à **Home Assistant**.

## Installation

1. Installez via HACS.
2. Ajoutez l'intégration dans Paramètres > Appareils.
3. Pour changer l'IP/port/identifiants/circuits actifs par la suite, utilisez
   **Reconfigurer** sur l'intégration — pas besoin de la supprimer.

## Fonctionnalités

* Gestion du Chauffage (Climate)
* Gestion de l'Eau Chaude (Water Heater), avec cycle anti-légionellose
* Programmation horaire (Calendar)
* Capteurs de surveillance (Sensor), y compris les courbes de chauffe et
  noms de circuits configurés sur le panneau physique
* Lecture groupée des paramètres (plusieurs valeurs par trame réseau) pour
  un polling plus rapide et plus fiable
* Diagnostics téléchargeables depuis l'UI HA (Paramètres > Appareils >
  Plum EcoMAX > Télécharger les diagnostics)

## Notes importantes

* **`Force pompe ECS → ballon solaire`** (et tout paramètre de "forçage")
  n'a d'effet physique que si le panneau de la chaudière est en **mode
  manuel** — c'est une limitation du firmware de la chaudière, pas de
  l'intégration.
* Seuls les circuits sélectionnés lors de la configuration
  (`Circuits de chauffage actifs`) génèrent des entités.