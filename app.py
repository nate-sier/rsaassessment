#!/usr/bin/env python3
"""Authenticated Streamlit entrypoint for GitHub/Community Cloud deployment."""
import hashlib
import hmac
import os
import runpy
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
from workspace_portability import backup_workspace, default_locations, restore_workspace

ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title='STATSports | Three-set sprint review', layout='wide')


def read_setting(key: str) -> str:
    """Secrets override ordinary environment variables; don't expose values in UI."""
    try:
        found = st.secrets.get(key, None)
        if found is not None:
            return str(found).strip()
    except (FileNotFoundError, OSError, KeyError):
        pass
    return os.environ.get(key, '').strip()


for setting in ('STATSPORTS_API_KEY', 'APP_PASSWORD', 'STATSPORTS_CLOUD_MODE',
                'STATSPORTS_DATA_ROOT', 'STATSPORTS_REVIEW_ROOT'):
    value = read_setting(setting)
    if value:
        os.environ[setting] = value

password = read_setting('APP_PASSWORD')
if not password or password.startswith('REPLACE_') or len(password) < 12:
    st.error('Set a private APP_PASSWORD of at least 12 characters in Streamlit secrets before using this app. Placeholder passwords are not accepted.')
    st.caption('See README.md for setup. This safeguard prevents accidentally publishing athlete GPS records in an unprotected app.')
    st.stop()

if st.session_state.get('statsports_password_version') != hashlib.sha256(password.encode()).hexdigest():
    st.session_state['statsports_authenticated'] = False
if not st.session_state.get('statsports_authenticated', False):
    st.title('STATSports | Private assessment review')
    entered = st.text_input('App access password', type='password', key='statsports_password')
    if st.button('Unlock app', type='primary'):
        if hmac.compare_digest(entered, password):
            st.session_state['statsports_authenticated'] = True
            st.session_state['statsports_password_version'] = hashlib.sha256(password.encode()).hexdigest()
            st.rerun()
        else:
            st.error('Incorrect password.')
    st.stop()

source, reviewed = default_locations(ROOT)
os.environ['STATSPORTS_DATA_ROOT'] = str(source)
os.environ['STATSPORTS_REVIEW_ROOT'] = str(reviewed)
with st.sidebar:
    st.caption('Private workspace · login required')
    if st.button('Lock app', use_container_width=True):
        st.session_state['statsports_authenticated'] = False
        st.session_state.pop('workspace_backup', None)
        st.rerun()
    if not read_setting('STATSPORTS_API_KEY') or read_setting('STATSPORTS_API_KEY').startswith('REPLACE_'):
        st.warning('No STATSports API credential is configured. Existing saved assessments can still be reviewed after restoring a workspace backup.')
    with st.expander('Workspace backup / restore', expanded=False):
        st.caption('Cloud files are temporary. Download a private workspace backup after a pull or after manual edits. The ZIP contains athlete performance data; keep it secure.')
        if st.button('Prepare private workspace backup'):
            try:
                st.session_state['workspace_backup'] = backup_workspace(source, reviewed)
                st.success('Workspace backup prepared. Download it below.')
            except (ValueError, OSError) as err:
                st.error(str(err))
        data = st.session_state.get('workspace_backup')
        if data:
            st.download_button('Download workspace ZIP', data=data,
                file_name=f'STATSports_Private_Backup_{datetime.now(timezone.utc):%Y%m%d}.zip',
                mime='application/zip', use_container_width=True)
        uploaded = st.file_uploader('Restore a previously downloaded workspace ZIP', type=['zip'], key='workspace_restore_upload')
        if uploaded is not None and st.button('Restore workspace', type='secondary'):
            try:
                n = restore_workspace(uploaded.getvalue(), source, reviewed)
                st.success(f'Restored {n} files. Rerunning app…')
                st.rerun()
            except Exception as err:
                st.error('Restore blocked: ' + str(err))

# Navigation is above the date-specific review flow, so norms remain accessible
# even before a particular day's raw GPS data has been downloaded.
with st.sidebar:
    page = st.radio('App page', ['Assessment review', 'Normative quadrants'], key='app_page')

if page == 'Normative quadrants':
    from normative_quadrants_v39 import render
    render()
else:
    # Run existing review on every rerun, preserving its saved correction logic.
    runpy.run_path(str(ROOT / 'manual_review_app.py'), run_name='__main__')
