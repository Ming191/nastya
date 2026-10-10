"use client";

import {
  Room, RoomEvent, Track,
  type LocalVideoTrack, type RemoteAudioTrack, type RemoteVideoTrack,
} from "livekit-client";
import { useCallback, useEffect, useRef, useState } from "react";
import { getMediaError, isHumanIdentity, languageLabel, type ParticipantRole, type SpokenLanguage } from "../lib/call";

type DeviceKind = "audioinput" | "videoinput" | "audiooutput";
type VideoTrack = LocalVideoTrack | RemoteVideoTrack;

function VideoTile({
  track, label, local, available,
}: {
  track?: VideoTrack;
  label: string;
  local: boolean;
  available: boolean;
}) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || !track) return;
    track.attach(el);
    return () => {
      track.detach(el);
      el.srcObject = null;
    };
  }, [track]);

  return <article className={"video-tile" + (local ? " self" : "")}>
    <video ref={ref} autoPlay playsInline muted={local} aria-label={label + " video"} />
    {!available && <div className="video-empty" role="status">
      {local ? "Your camera is off" : "Waiting for remote video"}
    </div>}
    <div className="tile-label">{label}</div>
  </article>;
}

function RemoteAudio({ track, volume }: { track?: RemoteAudioTrack; volume: number }) {
  const ref = useRef<HTMLAudioElement>(null);
  useEffect(() => {
    const audio = ref.current;
    if (!track || !audio) return;
    track.attach(audio);
    audio.volume = volume;
    // Playback may require an explicit user gesture on mobile/Safari.
    void audio.play().catch(() => {});
    return () => {
      track.detach(audio);
      audio.srcObject = null;
    };
  }, [track]);
  useEffect(() => {
    if (ref.current) ref.current.volume = volume;
  }, [volume]);
  return <audio ref={ref} autoPlay className="audio-sink" aria-label="Other participant audio" />;
}

function deviceName(device: MediaDeviceInfo, index: number) {
  const type = device.kind === "audioinput" ? "Microphone" :
    device.kind === "videoinput" ? "Camera" : "Speaker";
  return device.label || type + " " + (index + 1);
}

export function LiveCall({
  room, role, language, onLeave, onCopyInvite, canShare,
}: {
  room: Room;
  role: ParticipantRole;
  language: SpokenLanguage;
  onLeave: () => Promise<void>;
  onCopyInvite: () => Promise<void>;
  canShare: boolean;
}) {
  const [revision, setRevision] = useState(0);
  const [connection, setConnection] = useState<"connected" | "reconnecting" | "disconnected">("connected");
  const [mediaErrors, setMediaErrors] = useState<{ microphone?: string; camera?: string }>({});
  const [action, setAction] = useState<string | null>(null);
  const [outputError, setOutputError] = useState("");
  const [volume, setVolume] = useState(1);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [selectedDevices, setSelectedDevices] = useState<Record<DeviceKind, string>>({
    audioinput: "", videoinput: "", audiooutput: "",
  });

  const refreshDevices = useCallback(async () => {
    try {
      if (!navigator.mediaDevices?.enumerateDevices) return;
      setDevices(await navigator.mediaDevices.enumerateDevices());
    } catch {
      // Device enumeration can be unavailable without HTTPS or browser permission.
    }
  }, []);

  useEffect(() => {
    let active = true;
    const refresh = () => {
      if (active) setRevision((value) => value + 1);
    };
    const reconnecting = () => { if (active) setConnection("reconnecting"); };
    const reconnected = () => {
      if (active) {
        setConnection("connected");
        refresh();
      }
    };
    const disconnected = () => { if (active) setConnection("disconnected"); };
    const onDevices = () => { void refreshDevices(); };

    room.on(RoomEvent.ParticipantConnected, refresh);
    room.on(RoomEvent.ParticipantDisconnected, refresh);
    room.on(RoomEvent.TrackPublished, refresh);
    room.on(RoomEvent.TrackUnpublished, refresh);
    room.on(RoomEvent.TrackSubscribed, refresh);
    room.on(RoomEvent.TrackUnsubscribed, refresh);
    room.on(RoomEvent.TrackMuted, refresh);
    room.on(RoomEvent.TrackUnmuted, refresh);
    room.on(RoomEvent.LocalTrackPublished, refresh);
    room.on(RoomEvent.LocalTrackUnpublished, refresh);
    room.on(RoomEvent.MediaDevicesChanged, onDevices);
    room.on(RoomEvent.Reconnecting, reconnecting);
    room.on(RoomEvent.Reconnected, reconnected);
    room.on(RoomEvent.Disconnected, disconnected);

    const startLocalMedia = async () => {
      // Capture each input independently: denial of either permission is non-fatal.
      const results = await Promise.allSettled([
        room.localParticipant.setMicrophoneEnabled(true, {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        }),
        room.localParticipant.setCameraEnabled(true),
      ]);
      if (!active) return;
      setMediaErrors({
        microphone: results[0].status === "rejected"
          ? getMediaError(results[0].reason, "microphone") : undefined,
        camera: results[1].status === "rejected"
          ? getMediaError(results[1].reason, "camera") : undefined,
      });
      await refreshDevices();
      refresh();
    };
    void startLocalMedia();
    void refreshDevices();

    return () => {
      active = false;
      room.off(RoomEvent.ParticipantConnected, refresh);
      room.off(RoomEvent.ParticipantDisconnected, refresh);
      room.off(RoomEvent.TrackPublished, refresh);
      room.off(RoomEvent.TrackUnpublished, refresh);
      room.off(RoomEvent.TrackSubscribed, refresh);
      room.off(RoomEvent.TrackUnsubscribed, refresh);
      room.off(RoomEvent.TrackMuted, refresh);
      room.off(RoomEvent.TrackUnmuted, refresh);
      room.off(RoomEvent.LocalTrackPublished, refresh);
      room.off(RoomEvent.LocalTrackUnpublished, refresh);
      room.off(RoomEvent.MediaDevicesChanged, onDevices);
      room.off(RoomEvent.Reconnecting, reconnecting);
      room.off(RoomEvent.Reconnected, reconnected);
      room.off(RoomEvent.Disconnected, disconnected);
    };
  }, [room, refreshDevices]);

  // revision is deliberately read to resnapshot SDK publications after each event.
  void revision;
  const localCam = room.localParticipant.getTrackPublication(Track.Source.Camera);
  const localMic = room.localParticipant.getTrackPublication(Track.Source.Microphone);
  const cameraOn = !!localCam?.videoTrack && !localCam.isMuted;
  const micOn = !!localMic?.audioTrack && !localMic.isMuted;
  const peer = Array.from(room.remoteParticipants.values())
    .find((participant) => isHumanIdentity(participant.identity));
  const peerCam = peer?.getTrackPublication(Track.Source.Camera);
  const peerMic = peer?.getTrackPublication(Track.Source.Microphone);
  const remoteCameraOn = !!peerCam?.videoTrack && !peerCam.isMuted;

  async function toggleInput(device: "microphone" | "camera") {
    setAction(device);
    try {
      if (device === "microphone") {
        await room.localParticipant.setMicrophoneEnabled(!micOn, {
          echoCancellation: true, noiseSuppression: true, autoGainControl: true,
          ...(selectedDevices.audioinput ? { deviceId: selectedDevices.audioinput } : {}),
        });
      } else {
        await room.localParticipant.setCameraEnabled(!cameraOn, {
          ...(selectedDevices.videoinput ? { deviceId: selectedDevices.videoinput } : {}),
        });
      }
      setMediaErrors((current) => ({ ...current, [device]: undefined }));
      await refreshDevices();
    } catch (error) {
      setMediaErrors((current) => ({ ...current, [device]: getMediaError(error, device) }));
    } finally {
      setAction(null);
      setRevision((value) => value + 1);
    }
  }

  async function selectDevice(kind: DeviceKind, id: string) {
    setAction(kind);
    setOutputError("");
    try {
      if (id && ((kind === "audioinput" && micOn) ||
        (kind === "videoinput" && cameraOn) || kind === "audiooutput")) {
        const success = await room.switchActiveDevice(kind, id);
        if (success === false) throw new Error("device not supported");
      }
      setSelectedDevices((previous) => ({ ...previous, [kind]: id }));
    } catch {
      setOutputError("Could not switch device. Check browser permissions and available devices.");
    } finally {
      setAction(null);
      setRevision((value) => value + 1);
    }
  }

  async function enableAudio() {
    setAction("audio");
    try {
      await room.startAudio();
      setOutputError("");
    } catch {
      setOutputError("Audio playback was blocked. Check browser sound settings.");
    } finally {
      setAction(null);
      setRevision((value) => value + 1);
    }
  }

  const kinds: Array<{ kind: DeviceKind; label: string }> = [
    { kind: "audioinput", label: "Microphone device" },
    { kind: "videoinput", label: "Camera device" },
    { kind: "audiooutput", label: "Speaker device" },
  ];
  return <div className="call-container">
    <header className="call-header">
      <div>
        <div className="eyebrow">NASTYA / PRIVATE CALL</div>
        <h1>Video call</h1>
        <p>You: {role} · {languageLabel(language)}</p>
      </div>
      <span className={"connection " + connection} role="status">{connection}</span>
    </header>

    <div className="video-grid" aria-label="Video call participants">
      <VideoTile
        track={peerCam?.videoTrack} label={peer ? "Other participant" : "Waiting for other participant"}
        local={false} available={remoteCameraOn}
      />
      <VideoTile
        track={localCam?.videoTrack} label="You" local available={cameraOn}
      />
    </div>
    <RemoteAudio track={peerMic?.audioTrack} volume={volume} />
    {!peer && <p className="hint" role="status">Waiting for the other person to join using the invitation link.</p>}
    {!room.canPlaybackAudio && <div className="call-alert">
      <p>Browser audio playback may be blocked.</p>
      <button type="button" onClick={enableAudio} disabled={action !== null}>Enable incoming sound</button>
    </div>}
    {connection === "disconnected" && <p role="alert">The call disconnected. Leave and join again to reconnect.</p>}
    {connection === "reconnecting" && <p role="status">Connection interrupted; trying to reconnect…</p>}
    {mediaErrors.microphone && <p role="alert">{mediaErrors.microphone}</p>}
    {mediaErrors.camera && <p role="alert">{mediaErrors.camera}</p>}
    {outputError && <p role="alert">{outputError}</p>}

    <div className="call-controls" aria-label="Call controls">
      <button type="button" onClick={() => void toggleInput("microphone")}
        aria-pressed={micOn} disabled={action !== null || connection === "disconnected"}>
        {micOn ? "Mute mic" : "Turn mic on"}
      </button>
      <button type="button" onClick={() => void toggleInput("camera")}
        aria-pressed={cameraOn} disabled={action !== null || connection === "disconnected"}>
        {cameraOn ? "Turn camera off" : "Turn camera on"}
      </button>
      <button type="button" onClick={() => void onCopyInvite()} disabled={!canShare}>Copy invite link</button>
      <button type="button" className="danger" onClick={() => void onLeave()}>Leave call</button>
    </div>

    <section className="device-panel" aria-label="Call device settings">
      {kinds.map(({ kind, label }) => {
        const choices = devices.filter((device) => device.kind === kind && device.deviceId);
        return <label key={kind} className="device-field">
          <span>{label}</span>
          <select value={selectedDevices[kind]}
            disabled={action !== null || (kind === "audiooutput" && choices.length === 0)}
            onChange={(event) => void selectDevice(kind, event.target.value)}>
            <option value="">System default</option>
            {choices.map((device, index) =>
              <option key={device.deviceId} value={device.deviceId}>{deviceName(device, index)}</option>
            )}
          </select>
        </label>;
      })}
      <label className="device-field">
        <span>Remote volume: {Math.round(volume * 100)}%</span>
        <input type="range" min="0" max="1" step="0.05"
          value={volume} onChange={(event) => setVolume(Number(event.target.value))}
          aria-label="Remote speaker volume" />
      </label>
    </section>
  </div>;
}
