from django.urls import reverse
from .access import ROLE_ADMIN, ROLE_GESTIONNAIRE, restricted_section, user_has_role


def navigation_roles(request):
    user = request.user
    can_manage_business = user_has_role(user, ROLE_GESTIONNAIRE, ROLE_ADMIN)
    can_manage_users = user_has_role(user, ROLE_ADMIN)
    limited = restricted_section(user)
    resolver = request.resolver_match
    current_name = resolver.url_name if resolver else ''
    definitions = [
        ('dashboard','Tableau de bord','dashboard','/tableau-de-bord/'),
        ('document_deposit','Déposer un document','deposit','/documents/deposer/'),
        ('work_documents','Données à vérifier','check','/documents/'),
        ('price_review','Revue des prix','price','/revue-prix/'),
        ('reconciliation_queue','Rapprochement','reconcile','/rapprochement/'),
        ('delivery_issue_review','Problèmes de livraison','delivery','/problemes-livraison/'),
        ('supplier_claims','Réclamations','claim','/reclamations/'),
        ('catalogue_coverage','Catalogues','catalogue','/catalogues/'),
        ('restaurant_list','Restaurants','restaurant','/restaurants/'),
        ('fournisseur_list','Fournisseurs','supplier','/fournisseurs/'),
        ('audit_log','Journal','journal','/journal/'),
        ('document_archives','Archives','archive','/archives/'),
        ('user_list','Utilisateurs','users','/utilisateurs/'),
        ('ai_diagnostic','Analyse IA','ai','/diagnostic-ia/'),
    ]
    descriptions = {
        'document_deposit':('documents','Prenez une photo ou ajoutez vos factures et bons de livraison.'),
        'work_documents':('admin','Vérifiez et complétez les informations extraites des documents.'),
        'price_review':('daily','Traitez les écarts entre les prix facturés et les catalogues.'),
        'reconciliation_queue':('daily','Comparez les livraisons et les factures, puis validez les propositions.'),
        'delivery_issue_review':('daily','Vérifiez les défauts signalés lors de la réception des marchandises.'),
        'supplier_claims':('daily','Regroupez les écarts par fournisseur et suivez les avoirs.'),
        'catalogue_coverage':('reference','Consultez les produits et mettez à jour les prix de référence.'),
        'restaurant_list':('admin','Retrouvez vos établissements et leurs informations.'),
        'fournisseur_list':('reference','Gérez les fournisseurs et consultez leurs documents.'),
        'audit_log':('history','Retrouvez les actions effectuées et leur historique.'),
        'document_archives':('history','Consultez et restaurez les documents archivés.'),
        'user_list':('admin','Gérez les comptes, les rôles et les accès aux établissements.'),
        'ai_diagnostic':('admin','Configurez l’analyse IA et consultez son diagnostic.'),
    }
    sections=[]
    for name,label,icon,prefix in definitions:
        allowed = (can_manage_business and (name not in ('user_list','document_archives','ai_diagnostic') or can_manage_users)) or (limited=='deposit' and name=='document_deposit') or (limited=='review' and name=='price_review')
        if not allowed: continue
        active = current_name==name or request.path.startswith(prefix)
        if name=='reconciliation_queue' and current_name in ('invoice_reconcile','delivery_as_invoice'): active=True
        if name=='work_documents': active = request.path.startswith('/documents/') and current_name!='document_deposit'
        group,description=descriptions.get(name,('',''))
        sections.append({'name':name,'url':reverse(name),'label':label,'icon':icon,'active':active,'group':group,'description':description})
    groups=[]
    for key,title,description in [('daily','Au quotidien','Déposer, contrôler et traiter les écarts.'),
            ('documents','Documents','Déposer les factures et bons de livraison.'),
            ('reference','Références','Les produits, les prix et les fournisseurs.'),
            ('history','Historique','Retrouver les documents et les décisions.'),
            ('admin','Administration','Les données, les établissements et les accès.')]:
        entries=[s for s in sections if s['group']==key]
        if entries: groups.append({'key':key,'title':title,'description':description,'sections':entries})
    page_headings = {
        'price_review':('CONTRÔLE FOURNISSEURS','Votre revue des prix'),
        'supplier_claims':('SUIVI FOURNISSEURS','Réclamations'),
        'reconciliation_queue':('CONTRÔLE DES LIVRAISONS','Rapprochement des factures'),
        'invoice_reconcile':('CONTRÔLE DES LIVRAISONS','Rapprochement des factures'),
        'delivery_issue_review':('CONTRÔLE DES LIVRAISONS','Problèmes de livraison'),
    }
    heading = page_headings.get(current_name, ('',''))
    if current_name=='invoice_reconcile' and request.GET.get('advanced')=='1':
        heading=('','')
    home_url=reverse('dashboard') if can_manage_business else sections[0]['url'] if sections else reverse('home')
    return {
        'nav_can_manage_business':can_manage_business,'nav_can_manage_users':can_manage_users,
        'nav_reception_only':limited=='deposit','nav_review_only':limited=='review',
        'nav_profile_role':'Admin' if can_manage_users else 'Gestionnaire' if can_manage_business else 'Revue' if limited=='review' else 'Réception' if limited=='deposit' else 'Compte',
        'header_eyebrow':heading[0],'header_title':heading[1],'nav_home_url':home_url,'nav_groups':groups,'nav_sections':sections,'nav_current':next((s for s in sections if s['active']),None),
    }
