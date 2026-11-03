"""Live Google Sheets normative scatter charts for authenticated STATSports app."""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from sheet_sync_v38 import HEADERS, SPREADSHEET_ID, WORKSHEET_GID, normal_date, normal_name, worksheet_from_secrets

NUMERIC_COLUMNS = HEADERS[2:]
SPEED_METRICS = ['Best mph', 'Top 3 mph', 'Mean mph']
DROP_METRICS = ['Peak to Avg Drop %', 'Best to Worst Drop %', 'Set 3 vs 1 Decline %', 'Speed CV %']


def parse_records(grid):
    """Read the same A:M schema used by the writer; reject malformed rows rather than quietly skew norms."""
    if not grid:
        raise ValueError('The Google worksheet is empty.')
    if [str(c).strip() for c in grid[0]] != HEADERS:
        raise ValueError('The Google worksheet headers must match the exact 13-column export format (A:M).')
    records = []
    failures = []
    for row_number, values in enumerate(grid[1:], start=2):
        if not any(str(v).strip() for v in values):
            continue
        values = list(values) + [''] * max(0, len(HEADERS)-len(values))
        try:
            day = normal_date(values[0])
            name = str(values[1]).strip()
            if not name:
                raise ValueError('empty athlete name')
            numeric = [float(str(v).replace(',', '').strip()) for v in values[2:len(HEADERS)]]
            if not np.isfinite(numeric).all() or any(n <= 0 for n in numeric[:4]):
                raise ValueError('missing, nonfinite, or nonpositive speed')
            records.append([day, name, *numeric])
        except (ValueError, TypeError) as exc:
            failures.append(f'Row {row_number}: {exc}')
    if failures:
        raise ValueError(f'{len(failures)} invalid sheet row(s). Fix them before calculating norms. '
                         + ' | '.join(failures[:8]))
    result = pd.DataFrame(records, columns=HEADERS)
    if result.empty:
        return result
    keys = result['Date'].astype(str) + '|' + result['Athlete'].map(normal_name)
    if keys.duplicated().any():
        repeated = result.loc[keys.duplicated(keep=False), ['Date', 'Athlete']]
        raise ValueError('Duplicate athlete + date in worksheet; resolve before normative analysis: '
                         + ', '.join(f"{r.Athlete} ({r.Date})" for r in repeated.itertuples(index=False))[:250])
    return result


def latest_per_athlete(frame):
    if frame.empty:
        return frame.copy()
    temp = frame.copy()
    temp['_identity'] = temp['Athlete'].map(normal_name)
    temp = temp.sort_values(['Date','Athlete'], kind='stable').drop_duplicates('_identity', keep='last')
    return temp.drop(columns='_identity').sort_values('Athlete').reset_index(drop=True)


def quadrant_assignments(frame, xmetric, ymetric, xmid, ymid):
    output = frame.copy()
    output['Quadrant'] = np.where(output[xmetric] >= xmid, 'High speed', 'Low speed') + ' | ' + np.where(
        output[ymetric] <= ymid, 'Low drop', 'High drop')
    return output


def scatter(frame, xmetric, ymetric, xmid, ymid, show_names=True):
    # Higher speed is good, lower percentage is good, for all supported y metrics.
    palette = {'High speed | Low drop':'#18846d', 'High speed | High drop':'#e1a73c',
               'Low speed | Low drop':'#4b82be', 'Low speed | High drop':'#c85a57'}
    fig = go.Figure()
    for group, color in palette.items():
        part = frame.loc[frame['Quadrant'].eq(group)]
        if part.empty:
            continue
        fig.add_trace(go.Scatter(
            x=part[xmetric], y=part[ymetric], mode='markers+text' if show_names else 'markers',
            name=group, text=part['Athlete'] if show_names else None,
            textposition='top center', textfont={'size':10},
            customdata=part[['Athlete','Date']].to_numpy(),
            hovertemplate='<b>%{customdata[0]}</b><br>Date: %{customdata[1]}'
                          f'<br>{xmetric}: %{{x:.2f}}<br>{ymetric}: %{{y:.2f}}<extra></extra>',
            marker={'color':color,'size':11,'opacity':0.88,'line':{'color':'white','width':0.7}},
        ))
    fig.add_vline(x=xmid, line_dash='dash', line_color='#888888')
    fig.add_hline(y=ymid, line_dash='dash', line_color='#888888')
    fig.update_layout(height=650, xaxis_title=xmetric, yaxis_title=ymetric,
                      legend_title='Quadrant', margin=dict(l=35,r=25,t=20,b=40),
                      hovermode='closest', template='plotly_white')
    # Reverse the y axis to show low drop (better maintenance) towards the top.
    fig.update_yaxes(autorange='reversed')
    return fig


def normative_table(frame, metrics):
    return pd.DataFrame({
        'Metric': metrics,
        'P10': [frame[m].quantile(.10) for m in metrics],
        'P25': [frame[m].quantile(.25) for m in metrics],
        'P50': [frame[m].quantile(.50) for m in metrics],
        'P75': [frame[m].quantile(.75) for m in metrics],
        'P90': [frame[m].quantile(.90) for m in metrics],
    })


def render():
    import streamlit as st
    st.title('3×10×10 | Normative Quadrants')
    st.caption('Live athlete summaries from the linked Google worksheet. The app reads the sheet only on this page; it does not change existing results.')
    st.caption(f'Google Sheets source: {SPREADSHEET_ID} · worksheet gid {WORKSHEET_GID}')
    if st.button('Refresh from Google Sheet', type='primary', key='quadrants_refresh'):
        st.session_state.pop('quadrants_sheet_data', None)
    if 'quadrants_sheet_data' not in st.session_state:
        try:
            with st.spinner('Reading complete normative database from Google Sheets…'):
                worksheet = worksheet_from_secrets(st.secrets)
                st.session_state['quadrants_sheet_data'] = parse_records(worksheet.get_all_values())
        except Exception as exc:
            st.error('Could not load normative data: ' + str(exc))
            return
    data = st.session_state['quadrants_sheet_data'].copy()
    if data.empty:
        st.info('There are no test results in the worksheet yet.')
        return
    st.info('For a true between-athlete comparison, select **Latest test per athlete**. All tests can be useful for tracking observations over time, but repeated tests then give some athletes more weight.')
    use_latest = st.radio('Observations', ['Latest test per athlete','All tests'], horizontal=True)
    if use_latest == 'Latest test per athlete':
        data = latest_per_athlete(data)
    c1,c2,c3 = st.columns([1,1,1])
    with c1:
        xmetric = st.selectbox('Speed metric (horizontal)', SPEED_METRICS, index=1)
    with c2:
        ymetric = st.selectbox('Drop/maintenance metric (vertical)', DROP_METRICS, index=0)
    with c3:
        show_names = st.checkbox('Show athlete labels', value=True)
    st.caption('Speed: farther right is faster. Drop/variability: higher on the plot is better (lower numeric value).')
    auto_x = float(data[xmetric].median()); auto_y = float(data[ymetric].median())
    use_custom = st.checkbox('Customize quadrant cutoffs', value=False)
    if use_custom:
        col_x,col_y = st.columns(2)
        xmid = col_x.number_input(f'{xmetric} cutoff', value=auto_x, step=0.1, format='%.3f')
        ymid = col_y.number_input(f'{ymetric} cutoff', value=auto_y, step=0.1, format='%.3f')
    else:
        xmid,ymid=auto_x,auto_y
    chartdata = quadrant_assignments(data,xmetric,ymetric,xmid,ymid)
    s1,s2,s3,s4=st.columns(4)
    s1.metric('Athletes', chartdata['Athlete'].map(normal_name).nunique())
    s2.metric('Tests', len(chartdata))
    s3.metric(f'Median {xmetric}', f'{auto_x:.2f}')
    s4.metric(f'Median {ymetric}', f'{auto_y:.2f}%')
    st.plotly_chart(scatter(chartdata,xmetric,ymetric,xmid,ymid,show_names=show_names), use_container_width=True)
    st.caption(f'Dashed lines: speed = {xmid:.2f} mph; drop = {ymid:.2f}%. '
               'Quadrants are descriptive, sample-relative categories—not validated fatigue diagnoses.')
    st.subheader('Quadrant counts')
    counts=chartdata['Quadrant'].value_counts().rename_axis('Quadrant').reset_index(name='Tests')
    st.dataframe(counts, hide_index=True, use_container_width=True)
    st.subheader('Athlete results')
    st.dataframe(chartdata[['Date','Athlete',xmetric,ymetric,'Quadrant']].sort_values([xmetric],ascending=False),
                 hide_index=True,use_container_width=True)
    st.download_button('Download plotted athletes (CSV)',data=chartdata.to_csv(index=False).encode('utf-8'),
                       file_name='3x10x10_quadrants.csv',mime='text/csv')
    st.subheader('Reference percentiles')
    st.caption('Percentiles are computed from the selected set of tests. Low percentiles in drop metrics represent lower drop.')
    st.dataframe(normative_table(data,SPEED_METRICS+DROP_METRICS).style.format(precision=2),
                 hide_index=True,use_container_width=True)
