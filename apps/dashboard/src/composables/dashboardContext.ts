import { type InjectionKey, inject, provide } from "vue";
import type { Dashboard } from "./useDashboard";

const dashboardKey: InjectionKey<Dashboard> = Symbol("Evidence Lab dashboard");
export function provideDashboard(dashboard: Dashboard) {
  provide(dashboardKey, dashboard);
}
export function useDashboardContext(): Dashboard {
  const dashboard = inject(dashboardKey);
  if (!dashboard) throw new Error("Dashboard components require the workspace context.");
  return dashboard;
}
