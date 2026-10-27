"""Validate a TrafficBot export offline; optionally save a reviewable normalized file."""
import argparse
import json
from pathlib import Path
from app.services.traffic_import import parse


def main():
    p=argparse.ArgumentParser()
    p.add_argument('file')
    for field in ('counter-id','point-id','timezone','stream-id'):
        p.add_argument('--'+field,required=True)
    p.add_argument('--output')
    args=p.parse_args()
    try:
        result=parse(Path(args.file).read_bytes(),args.counter_id,args.point_id,args.timezone,args.stream_id)
        if args.output:
            with Path(args.output).open('x',encoding='utf-8') as f:
                json.dump(result,f,ensure_ascii=False,indent=2)
        print(json.dumps({k:v for k,v in result.items() if k!='events'},ensure_ascii=False,indent=2))
    except (ValueError,OSError) as exc:
        print(json.dumps({'status':'ERROR','error':str(exc)},ensure_ascii=False))
        raise SystemExit(1)


if __name__=='__main__': main()
