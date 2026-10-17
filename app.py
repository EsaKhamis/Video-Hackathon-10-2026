"""Streamlit UI: live player, alert feed, Cosmos reasoning, search, agent chat.

    uv run streamlit run app.py
"""

import streamlit as st

from videoagent import tracing
from videoagent.agent import agent
from videoagent.config import settings
from videoagent.realtime.pipeline import run
from videoagent.reason.cosmos import ask_clip
from videoagent.schema import read_events
from videoagent.search.vast import search_footage

SEVERITY_ICON = {"info": "🔵", "warning": "🟠", "critical": "🔴"}
INCIDENTS = {"close_pass", "cut_in", "blocked_bike_lane"}

st.set_page_config(page_title="Helmet Cam Witness", layout="wide")
tracing.init()
st.title("Your helmet cam is a witness")


def render_feed(container) -> None:
    container.empty()
    with container.container():
        st.subheader("Alert feed")
        events = read_events()
        if not events:
            st.caption("No alerts yet.")
        for e in reversed(events[-20:]):
            with st.expander(
                f"{SEVERITY_ICON[e.severity]} {e.event} · {e.clip_id} · "
                f"{e.t_start:.0f}–{e.t_end:.0f}s · {e.confidence}"
            ):
                st.write(e.reasoning)
                st.json(e.model_dump(), expanded=False)


search, ask, live = st.tabs(["Search footage", "Ask the agent", "Live replay (local YOLO)"])

with live:
    left, right = st.columns([3, 2])
    with left:
        source = st.text_input("Video source", settings.video_source)
        use_cosmos = st.toggle("Send flagged clips to Cosmos", value=bool(settings.cosmos_base_url))
        start = st.button("Start stream", type="primary")
        frame_slot = st.empty()
        status = st.empty()
    feed_slot = right.empty()
    render_feed(feed_slot)

    if start:
        try:
            for t, frame, clip in run(source):
                frame_slot.image(frame, channels="BGR", caption=f"t = {t:.1f}s")
                if clip is None:
                    continue
                status.info(f"YOLO flagged {clip.clip_id} ({', '.join(clip.yolo_labels)})")
                if not use_cosmos:
                    continue
                with st.spinner("Cosmos reasoning..."):
                    event = ask_clip(clip)
                status.success(f"{event.event}: {event.reasoning}  →  {agent.handle_event(event)}")
                render_feed(feed_slot)
            status.info("Stream ended.")
        except Exception as e:
            status.error(str(e))

with search:
    q = st.text_input("Search past footage", placeholder="car passing very close to a cyclist in a bike lane")
    if q:
        results = search_footage(q, k=10)
        if not results:
            st.caption("No matches.")
        for r in results:
            where = " · ".join(str(r[f]) for f in ("location", "camera_id") if r.get(f))
            score = f" · score {r['score']}" if r.get("score") is not None else ""
            st.markdown(
                f"**{r['clip_id']}** · {r.get('t_start', 0):.0f}–{r.get('t_end', 0):.0f}s · {where}{score}"
            )
            st.write(r.get("description", ""))
            if st.button("Check with Cosmos", key=f"check-{r['clip_id']}"):
                with st.spinner("Cosmos reasoning..."):
                    try:
                        clip = agent.load_clip(r["clip_id"])
                        event = ask_clip(clip)
                        st.video(clip.path)
                        if event.event in INCIDENTS:
                            agent.send_alert(event)
                            st.success(f"{event.event} ({event.severity}): {event.reasoning} Added to alert feed.")
                        else:
                            st.info(f"{event.event}: {event.reasoning}")
                    except Exception as e:
                        st.error(str(e))

with ask:
    if "history" not in st.session_state:
        st.session_state.history = []
    for role, text in st.session_state.history:
        st.chat_message(role).markdown(text)
    if question := st.chat_input("Ask about the footage, or 'write a report'"):
        st.chat_message("user").markdown(question)
        with st.spinner("Thinking..."):
            try:
                answer = agent.chat(question)
            except Exception as e:
                answer = f"Error: {e}"
        st.chat_message("assistant").markdown(answer)
        st.session_state.history += [("user", question), ("assistant", answer)]
