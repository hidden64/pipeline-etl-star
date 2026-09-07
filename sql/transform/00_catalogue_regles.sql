-- =============================================================================
--  TRANSFORM 00 : Catalogue des règles de qualité
-- =============================================================================
--  Externaliser les règles dans une table, plutôt que de les enfouir dans le
--  code, sert trois objectifs concrets :
--    - un métier peut LIRE le catalogue et discuter des seuils sans lire de SQL ;
--    - on change la sévérité d'une règle sans redéployer ;
--    - le rapport de qualité (phase 7) se génère par simple jointure sur ce
--      catalogue, ce qui garantit qu'aucun rejet n'apparaît sans être expliqué.
--
--  Deux sévérités, et la distinction est FONDAMENTALE :
--
--    ERREUR        La ligne est ÉCARTÉE du fait. La donnée est inutilisable ou
--                  dangereuse (retard de capteur en vrac, horaires incohérents).
--                  La perdre est moins grave que de fausser une moyenne.
--
--    AVERTISSEMENT La ligne est CONSERVÉE, mais soit corrigée automatiquement,
--                  soit rattachée à un membre inconnu. On trace quand même,
--                  parce qu'un volume anormal d'avertissements est un signal.
--
--  Règle de conception : on ne rejette (ERREUR) que ce qu'on ne peut PAS
--  récupérer sans inventer de la donnée. Tout ce qui est récupérable de façon
--  déterministe est un AVERTISSEMENT.
-- =============================================================================

INSERT INTO rejet.regle (code_regle, libelle, severite, entite) VALUES
    -- ---- ERREURS : la ligne quitte le flux -------------------------------
    ('R001_DATE_ILLISIBLE',
     'Date d''exploitation absente ou non convertible en date',
     'ERREUR', 'realisation'),

    ('R002_HEURE_THEORIQUE_ILLISIBLE',
     'Heure théorique absente ou hors du format HH:MM:SS attendu',
     'ERREUR', 'realisation'),

    ('R003_RETARD_ABERRANT',
     'Retard hors des bornes physiques plausibles [-30 min, +2 h]',
     'ERREUR', 'realisation'),

    ('R004_COURSE_MANQUANTE',
     'Identifiant de course absent : la ligne ne peut être rattachée',
     'ERREUR', 'realisation'),

    ('R005_ETAT_INCONNU',
     'État de course non reconnu (ni réalisé, ni supprimé)',
     'ERREUR', 'realisation'),

    ('R006_INCOHERENCE_SUPPRESSION',
     'Course déclarée supprimée mais porteuse d''une heure réelle ou d''un retard',
     'ERREUR', 'realisation'),

    ('R007_REALISE_SANS_HEURE',
     'Course déclarée réalisée mais sans heure réelle exploitable',
     'ERREUR', 'realisation'),

    ('R008_SEQUENCE_INVALIDE',
     'Rang d''arrêt absent ou non entier',
     'ERREUR', 'realisation'),

    -- ---- AVERTISSEMENTS : la ligne est conservée -------------------------
    ('A101_ARRET_ORPHELIN',
     'Code d''arrêt absent du référentiel : rattaché au membre inconnu',
     'AVERTISSEMENT', 'realisation'),

    ('A102_METEO_ABSENTE',
     'Aucune météo pour ce créneau : rattaché au membre inconnu',
     'AVERTISSEMENT', 'realisation'),

    ('A103_DOUBLON_EXACT',
     'Ligne strictement identique à une autre : un seul exemplaire conservé',
     'AVERTISSEMENT', 'realisation'),

    ('A104_DOUBLON_DIVERGENT',
     'Même passage observé plusieurs fois avec des mesures différentes : '
     'dernière observation conservée',
     'AVERTISSEMENT', 'realisation'),

    ('A105_VITESSE_MANQUANTE',
     'Vitesse moyenne absente : mesure laissée à NULL',
     'AVERTISSEMENT', 'realisation')

ON CONFLICT (code_regle) DO UPDATE
    SET libelle = EXCLUDED.libelle,
        severite = EXCLUDED.severite,
        entite = EXCLUDED.entite;
