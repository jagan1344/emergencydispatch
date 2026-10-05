import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { LiveProvider } from "./hooks/useLive";
import Ambulances from "./pages/Ambulances";
import Analytics from "./pages/Analytics";
import Dashboard from "./pages/Dashboard";
import EmergencyDetails from "./pages/EmergencyDetails";
import Hospitals from "./pages/Hospitals";
import Incidents from "./pages/Incidents";
import LiveMap from "./pages/LiveMap";
import Login from "./pages/Login";
import NewEmergency from "./pages/NewEmergency";
import Simulation from "./pages/Simulation";
import Traffic from "./pages/Traffic";
import { getToken } from "./services/api";

function Protected() {
  if (!getToken()) return <Navigate to="/login" replace />;
  return <LiveProvider><Layout /></LiveProvider>;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<Protected />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/map" element={<LiveMap />} />
          <Route path="/emergencies" element={<Incidents />} />
          <Route path="/emergencies/new" element={<NewEmergency />} />
          <Route path="/emergencies/:id" element={<EmergencyDetails />} />
          <Route path="/ambulances" element={<Ambulances />} />
          <Route path="/hospitals" element={<Hospitals />} />
          <Route path="/traffic" element={<Traffic />} />
          <Route path="/analytics" element={<Analytics />} />
          <Route path="/simulation" element={<Simulation />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
