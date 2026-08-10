"""Vote d'ensemble — la méthode « 31 modèles votent », construite et mesurée.

L'idée de fond est réelle et vient de la météo : l'ECMWF fait tourner 51
membres perturbés et lit la dispersion du résultat. Un vote à supermajorité
avec abstention est du reste de l'apprentissage automatique standard.

Ce paquet la construit telle qu'elle est décrite, puis pose la seule question
qui décide : **ces membres sont-ils indépendants ?** Un ensemble ne vaut que si
ses membres le sont. Trente-et-un modèles qui lisent la même série ne sont pas
trente-et-un avis : un vote 28/31 sur des modèles corrélés mesure sa propre
corrélation, pas une preuve. C'est un écho, et il est d'autant plus convaincant
qu'il est fort.
"""
