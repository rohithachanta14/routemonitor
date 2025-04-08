"""Route Timeline page — track when a prefix stabilized after a BGP event."""
import httpx
import pandas as pd
import plotly.express as px
import streamlit as st


def render(client) -> None:
    """Render the Route Timeline page."""
    st.title("📡 Route Timeline")
    st.caption("Track when a prefix stabilized after a BGP event")

    try:
        speakers = client.list_speakers()
    except httpx.HTTPStatusError as e:
        st.error(f"API error {e.response.status_code}: {e.response.text}")
        return
    except httpx.ConnectError:
        st.error("Cannot connect to RouteMonitor API. Is Docker running?")
        return

    speaker_options = {
        f"{s['hostname']} ({str(s['id'])[:8]})": s["id"] for s in speakers
    }

    col1, col2, col3 = st.columns(3)
    with col1:
        selected_name = st.selectbox(
            "BGP Speaker", options=["(all)"] + list(speaker_options.keys())
        )
    with col2:
        prefix = st.text_input("Prefix (CIDR)", placeholder="e.g. 10.0.0.0/24")
    with col3:
        time_range = st.selectbox("Time Range", options=["1h", "24h", "7d"], index=1)

    if not st.button("Fetch Timeline"):
        return

    speaker_id = (
        speaker_options.get(selected_name) if selected_name != "(all)" else None
    )

    try:
        events = client.get_route_events(
            speaker_id=speaker_id,
            prefix=prefix or None,
            limit=1000,
        )
    except httpx.HTTPStatusError as e:
        st.error(f"API error {e.response.status_code}: {e.response.text}")
        return
    except httpx.ConnectError:
        st.error("Cannot connect to RouteMonitor API. Is Docker running?")
        return

    if not events:
        st.warning("No route events found for the selected filters.")
        return

    df = pd.DataFrame(events)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["event_num"] = df["event_type"].map(
        {"UPDATE": 1, "WITHDRAW": 0, "STATE_CHANGE": 0.5}
    )

    flap_count = len(df[df["event_type"] == "WITHDRAW"])
    update_count = len(df[df["event_type"] == "UPDATE"])
    time_span = (df["timestamp"].max() - df["timestamp"].min()).total_seconds()

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Events", len(df))
    k2.metric("Updates", update_count)
    k3.metric("Withdrawals", flap_count)
    k4.metric("Time Span", f"{time_span / 60:.1f}m")

    fig = px.scatter(
        df,
        x="timestamp",
        y="event_num",
        color="neighbor_ip",
        symbol="event_type",
        hover_data=["prefix", "neighbor_asn", "event_type"],
        title=f"Route Events{' — ' + prefix if prefix else ''}",
        labels={"event_num": "Event Type", "timestamp": "Time"},
    )
    fig.update_yaxes(
        tickvals=[0, 0.5, 1],
        ticktext=["WITHDRAW", "STATE_CHANGE", "UPDATE"],
    )
    fig.update_layout(height=400)
    st.plotly_chart(fig, use_container_width=True)

    withdrawals = df[df["event_type"] == "WITHDRAW"].copy()
    if len(withdrawals) > 0:
        withdrawals = (
            withdrawals.set_index("timestamp").resample("1min").size().reset_index()
        )
        withdrawals.columns = ["time", "flap_count"]
        fig2 = px.bar(
            withdrawals,
            x="time",
            y="flap_count",
            title="Withdrawals per Minute",
            color_discrete_sequence=["#e74c3c"],
        )
        fig2.update_layout(height=250)
        st.plotly_chart(fig2, use_container_width=True)

    with st.expander("Raw Events Table"):
        display_cols = [
            "timestamp",
            "event_type",
            "prefix",
            "neighbor_ip",
            "neighbor_asn",
        ]
        st.dataframe(
            df[display_cols].sort_values("timestamp", ascending=False),
            height=300,
        )
