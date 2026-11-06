import ast, pathlib, tempfile
from unittest.mock import patch
source=pathlib.Path(__file__).with_name('app.py').read_text()
mod=ast.parse(source)
selected={'natural_key','playlist_key','add_sorted_files','recover_or_skip','command','validate','defaults'}
body=[node for node in mod.body if isinstance(node,ast.FunctionDef) and node.name in selected]
ns={'re':__import__('re'),'Path':pathlib.Path,'MAX_RETRIES_PER_VIDEO':3,'retry_counts':{},'lock':__import__('threading').RLock(),'states':{},'errors':{},'HOME':pathlib.Path(tempfile.mkdtemp())}
exec(compile(ast.Module(body=body,type_ignores=[]),'<functions>','exec'),ns)
assert sorted(['11.mp4','2.mp4','1.mp4','10.mp4'],key=ns['natural_key'])==['1.mp4','2.mp4','10.mp4','11.mp4']
c={'files':['11.mp4','2.mp4'],'index':0};ns['add_sorted_files'](c,['1.mp4','2.mp4']);assert c['files']==['1.mp4','2.mp4','11.mp4'] and c['index']==2
c=ns['defaults'](0);c['files']=['a.mp4','b.mp4'];ns['channels']=[c];ns['save']=lambda:None;ns['set_state']=lambda *args:None;ns['log_channel_error']=lambda *args:None;ns['wait_for_retry']=lambda *args:True
for attempt in range(1,3):
 assert ns['recover_or_skip'](0,c,'a.mp4','bad')
 assert c['index']==0,attempt
assert ns['recover_or_skip'](0,c,'a.mp4','bad') and c['index']==1
print('PASS natural sort, dedupe/current index, 3 retries then next video')
