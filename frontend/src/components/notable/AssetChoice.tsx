import { useTranslation } from "react-i18next";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

export function AssetChoice({ value, choose }: { value: "equities" | "all"; choose: (value: "equities" | "all") => void }) {
  const { t } = useTranslation();
  return <ToggleGroup type="single" size="sm" variant="outline" spacing={0} value={value}
    data-filter="assets" aria-label={t("ui.notable.assets.label")}
    onValueChange={(next) => next && choose(next as typeof value)}>
    <ToggleGroupItem value="equities" className="px-2.5">{t("ui.notable.assets.equities")}</ToggleGroupItem>
    <ToggleGroupItem value="all" className="px-2.5">{t("ui.notable.assets.all")}</ToggleGroupItem>
  </ToggleGroup>;
}
