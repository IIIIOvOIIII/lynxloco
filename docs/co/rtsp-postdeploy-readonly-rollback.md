# Rollback for read-only Miloco RTSP post-deploy regression inspection

No server-side rollback is required because this CO authorizes inspection only.

If any planned command would edit files, restart services, change configuration, delete data, alter credentials, capture/persist camera media, or mutate databases outside normal read-only API access, stop immediately and open a separate implementation CO.

If temporary local-only diagnostic files are created, delete only those exact files before closure. Preserve existing production application state.
