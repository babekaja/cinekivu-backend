#!/usr/bin/env python3
"""Vérification réelle du serveur démarré (utiliser sur une machine avec réseau)."""
import argparse
import sys
import httpx


def main():
    parser = argparse.ArgumentParser(description='Audit réseau lecture seule de CineKivu')
    parser.add_argument('--base', default='http://127.0.0.1:8000')
    parser.add_argument('--id', help='Identifiant de film pour tester les métadonnées')
    parser.add_argument('--search', default='Naruto', help='Mot-clé à tester pour les suggestions')
    parser.add_argument('--stream', action='store_true', help='Teste aussi la découverte des flux pour --id (coûteux)')
    args = parser.parse_args()
    checks = [('/health',None),('/api/v1/cinekivu/health',None),('/api/v1/home',None),
              ('/api/v1/catalog/movies',{'page':1}),
              ('/api/v1/catalog/series',{'page':1}),
              ('/api/v1/search',{'q':args.search,'edition':'fr'}),
              ('/api/v1/search/autocomplete',{'q':args.search}),
              ('/api/v1/suggestions',{'q':args.search})]
    if args.id:
        checks.extend([(f'/api/v1/titles/{args.id}/recommendations',None),
                       (f'/api/v1/titles/{args.id}/similar',None),
                       (f'/api/v1/cinekivu/titles/{args.id}',None),
                       (f'/api/v1/cinekivu/titles/{args.id}/subtitles',None)])
        if args.stream: checks.append((f'/api/v1/cinekivu/titles/{args.id}/streams',None))
    failures=0
    with httpx.Client(timeout=120, follow_redirects=False) as client:
        for path, params in checks:
            try:
                res=client.get(args.base.rstrip('/')+path,params=params)
                data=res.json()
                content=data.get('data',data) if isinstance(data,dict) else {}
                if path.endswith('/home') and isinstance(content,dict):
                    detail=f"sections={len(content.get('sections',[]))} source={content.get('source')}"
                elif path.endswith('/streams'):
                    detail=f"playable_count={content.get('playable_count', 0)}"
                elif 'recommendations' in path and isinstance(content,dict):
                    detail=f"sections={len(content.get('sections', []))} cast={len(content.get('cast', []))} partial={content.get('partial', False)}"
                elif 'subtitles' in path:
                    detail=f"captions={len(content.get('items', []))}"
                elif isinstance(content,dict) and 'items' in content:
                    detail=f"items={len(content['items'])}"
                else: detail='response JSON reçue'
                print(f'{res.status_code:3} {path} : {detail}')
                if res.status_code >= 400: failures+=1
            except (httpx.HTTPError,ValueError) as exc:
                print(f'ERR {path} : {type(exc).__name__} (connexion, DNS ou JSON)')
                failures+=1
    print(f'Bilan : {len(checks)-failures}/{len(checks)} requêtes réussies')
    return 1 if failures else 0


if __name__ == '__main__': sys.exit(main())
