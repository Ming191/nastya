export type HealthStatus = {
  service: "web";
  status: "ok";
  aiReady: false;
};

export function getHealthStatus(): HealthStatus {
  return { service: "web", status: "ok", aiReady: false };
}
