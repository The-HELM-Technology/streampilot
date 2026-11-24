import os, argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-port', type=str, help='port for the server to listen on', required=False)
    parser.add_argument('-name', type=str, help='name to identify the user', required=False)
    parser.add_argument('-max_streamhubs', type=str, help='maximum number of Streamhubs to monitor', required=False)
    parser.add_argument('-user', type=str, help='login username (default: admin)', required=False)
    parser.add_argument('-password', type=str, help='login password (default: admin)', required=False)
    parser.add_argument('-max_srtgateway', type=str,
                        help='maximum number of SRT Gateways to monitor (default: unlimited)', required=False)
    parser.add_argument('-srt_retention_days', type=str,
                        help='number of days to keep SRT Gateway samples and events (default: 30)', required=False)
    parser.add_argument('-streamhub_log_retention_days', type=str,
                        help='days to keep StreamHub logs (default: 0 = keep forever)', required=False)
    parser.add_argument('-db_url', type=str,
                        help='database URL: sqlite:///path/to/mini.db or postgresql://user:pass@host/db '
                             '(default: mini.db next to the package; Postgres needs the [postgres] extra)',
                        required=False)
    parser.add_argument('-mode', type=str, choices=['http', 'proxy'],
                        help='http = direct (default) | proxy = behind HTTPS reverse proxy', required=False)
    parsed_args = vars(parser.parse_args())

    if parsed_args.get('port'):
        # run() reads SP_PORT (this used to set an env var that nothing reads, so -port was ignored)
        if not parsed_args['port'].isdigit() or not (0 < int(parsed_args['port']) < 65536):
            parser.error(f"-port must be a number between 1 and 65535, got {parsed_args['port']!r}")
        os.environ['SP_PORT'] = parsed_args['port']
    if parsed_args.get('name'):
        os.environ['CLIENT_NAME'] = parsed_args['name']
    if parsed_args.get('max_streamhubs'):
        os.environ['MAX_STREAMHUB'] = parsed_args['max_streamhubs']
    if parsed_args.get('user'):
        os.environ['SP_USER'] = parsed_args['user']
    if parsed_args.get('password'):
        os.environ['SP_PASSWORD'] = parsed_args['password']
    if parsed_args.get('max_srtgateway'):
        os.environ['MAX_SRTGATEWAY'] = parsed_args['max_srtgateway']
    if parsed_args.get('srt_retention_days'):
        os.environ['SRT_RETENTION_DAYS'] = parsed_args['srt_retention_days']
    if parsed_args.get('streamhub_log_retention_days'):
        os.environ['STREAMHUB_LOG_RETENTION_DAYS'] = parsed_args['streamhub_log_retention_days']
    if parsed_args.get('mode'):
        os.environ['SP_MODE'] = parsed_args['mode']
    if parsed_args.get('db_url'):
        os.environ['STREAMPILOT_DB_URL'] = parsed_args['db_url']

    # Import after the environment is set: server.py opens the database at import time.
    from .server import run
    os.sys.exit(run())
