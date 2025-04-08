"""Anomaly Timeline page — history of detected routing anomalies."""
import httpx
import pandas as pd
import plotly.express as px
import streamlit as st

from dashboard.utils.formatting import anomaly_type_label, format_datetime

SEVERITY_ICONS = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵"}


def render(client) -> None:
    """Render the Anomaly Timeline page."""
    st.title("⚠️ Anomaly Timeline")
    st.caption("Detected routing anomalies and their history")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        severity_filter = st.multiselect(
            "Severity",
            options=["INFO", "WARNING", "CRITICAL"],
            default=["WARNING", "CRITICAL"],
        )
    with col2:
        type_filter = st.multiselect(
            "Anomaly Type",
            options=[
                "ROUTE_FLAP",
                "PATH_INSTABILITY",
                "CONVERGENCE_DELAY",
                "UNUSUAL_CHURN",
                "CORRELATED_FAILURE",
            ],
        )
    with col3:
        time_range = st.selectbox(
            "Time Range", options=["1h", "24h", "7d", "30d"], index=1
        )
    with col4:
        show_acked = st.checkbox("Show Acknowledged", value=False)

    if st.button("🔄 Refresh"):
        st.cache_data.clear()
        st.rerun()

    try:
        anomalies = client.list_anomalies(
            time_range=time_range,
            acknowledged=None if show_acked else False,
        )
    except httpx.HTTPStatusError as e:
        st.error(f"API error {e.response.status_code}: {e.response.text}")
        return
    except httpx.ConnectError:
        st.error("Cannot connect to RouteMonitor API. Is Docker running?")
        return

    if not anomalies:
        st.success("✅ No anomalies detected in this time range.")
        return

    df = pd.DataFrame(anomalies)
    df["detected_at"] = pd.to_datetime(df["detected_at"])

    if severity_filter:
        df = df[df["severity"].isin(severity_filter)]
    if type_filter:
        df = df[df["anomaly_type"].isin(type_filter)]

    if df.empty:
        st.info("No anomalies match the selected filters.")
        return

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Anomalies", len(df))
    k2.metric("Critical", len(df[df["severity"] == "CRITICAL"]))
    k3.metric("Warning", len(df[df["severity"] == "WARNING"]))
    k4.metric("Unacknowledged", len(df[~df["acknowledged"]]))

    df_hour = df.set_index("detected_at").resample("1h").size().reset_index()
    df_hour.columns = ["hour", "count"]
    fig = px.bar(
        df_hour,
        x="hour",
        y="count",
        title="Anomalies per Hour",
        color_discrete_sequence=["#e74c3c"],
    )
    fig.update_layout(height=250, margin=dict(t=40, b=20))
    st.plotly_chart(fig, use_container_width=True)

    st.divider()

    for _, row in df.sort_values("detected_at", ascending=False).iterrows():
        icon = SEVERITY_ICONS.get(row["severity"], "⚪")
        label = anomaly_type_label(row["anomaly_type"])
        prefix_str = row.get("prefix") or "system-wide"
        acked_str = "✅ Acked" if row.get("acknowledged") else "❌ Unacked"
        header = (
            f"{icon} **{label}** — `{prefix_str}` — {acked_str} — "
            f"{format_datetime(row['detected_at'])}"
        )

        with st.expander(header):
            details = row.get("details") or {}
            d1, d2 = st.columns(2)
            with d1:
                st.write(f"**Anomaly ID:** `{row['id']}`")
                st.write(f"**Type:** {label}")
                st.write(f"**Severity:** {row['severity']}")
                st.write(f"**Prefix:** {prefix_str}")
            with d2:
                if "z_score" in details:
                    st.metric("Z-Score", f"{details['z_score']:.2f}")
                if "isolation_forest_score" in details:
                    st.metric("IF Score", f"{details['isolation_forest_score']:.4f}")
                if "affected_prefix_count" in details:
                    st.metric("Affected Prefixes", details["affected_prefix_count"])

            if details.get("affected_prefixes"):
                st.write(
                    "**Affected prefixes:**",
                    ", ".join(details["affected_prefixes"][:10]),
                )

            if not row.get("acknowledged"):
                if st.button("Acknowledge", key=f"ack_{row['id']}"):
                    try:
                        client.acknowledge_anomaly(row["id"])
                        st.success("Acknowledged!")
                        st.rerun()
                    except httpx.HTTPStatusError as e:
                        st.error(
                            f"API error {e.response.status_code}: {e.response.text}"
                        )
