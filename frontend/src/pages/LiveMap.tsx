import { useState } from "react";
import OpsMap, { ALL_LAYERS, Layers, MapLegend } from "../map/OpsMap";
import { useLive } from "../hooks/useLive";

export default function LiveMap() {
  const [layers, setLayers] = useState<Layers>(ALL_LAYERS);
  const { roads, routes } = useLive();
  return (
    <div className="page full">
      <div className="toolbar">
        <h1>Live map</h1>
        {(Object.keys(layers) as (keyof Layers)[]).map((k) => (
          <label key={k} className="toggle"><input type="checkbox" checked={layers[k]} onChange={(e) => setLayers({ ...layers, [k]: e.target.checked })} /> {k}</label>
        ))}
        <span className="muted">{routes.length} active routes · {roads.length} congested/blocked roads</span>
      </div>
      <div className="map-full"><OpsMap layers={layers} /></div>
      <MapLegend />
    </div>
  );
}
