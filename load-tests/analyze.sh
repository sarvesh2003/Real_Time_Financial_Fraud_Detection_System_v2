#!/bin/bash
DIR=${1:-$(ls -td results_* 2>/dev/null | head -1)}
[ ! -d "$DIR" ] && echo "ERROR: Directory not found" && exit 1
cd "$DIR"
python3 << 'PY'
import json
def p(f):
 try:
  d=json.load(open(f));s=d.get('statusCodeDistribution',{});e=sum(v for k,v in s.items() if k!='OK');c=d['count'];p99=next((x['latency']/1e6 for x in d.get('latencyDistribution',[]) if x['percentage']==99),0);return {'c':c,'r':d['rps'],'a':d['average']/1e6,'p':p99,'s':100-e/c*100}
 except:return None
print('\n'+'='*80)
print('LOAD TEST RESULTS')
print('='*80+'\n')
print('{:15} | {:>10} | {:>10} | {:>12} | {:>12} | {:>10}'.format('Test','Requests','RPS','Avg Latency','P99 Latency','Success'))
print('-'*80)
for f,n in[('1k.json','1K'),('10k.json','10K'),('50k.json','50K'),('100k.json','100K'),('sustained.json','Sustained 60s')]:
 x=p(f)
 if x:print('{:15} | {:>10,} | {:>10.0f} | {:>10.2f}ms | {:>10.2f}ms | {:>9.2f}%'.format(n,x['c'],x['r'],x['a'],x['p'],x['s']))
u=p('sustained.json')
if u:print('\n'+'='*80+'\nRESUME SUMMARY\n'+'='*80+'\n\n  Sustained {:.0f} TPS | P99 {:.1f}ms | Success {:.2f}%\n  Total: {:,} requests in 60s\n'.format(u['r'],u['p'],u['s'],u['c']))
PY