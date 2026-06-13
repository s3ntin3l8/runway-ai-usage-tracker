// Display: theme preference (dark / light / system).

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Label } from '@/components/ui/Input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';
import { useTheme, type ThemePref } from '@/hooks/useTheme';

export function DisplaySection() {
  const { pref, setPref } = useTheme();

  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <CardTitle>Appearance</CardTitle>
      </CardHeader>
      <CardContent className="flex items-center justify-between gap-4">
        <div>
          <Label>Theme</Label>
          <p className="mt-0.5 text-[11px] text-fg-subtle">
            "System" follows your OS preference live.
          </p>
        </div>
        <Select value={pref} onValueChange={(v) => setPref(v as ThemePref)}>
          <SelectTrigger className="w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="dark">Dark</SelectItem>
            <SelectItem value="light">Light</SelectItem>
            <SelectItem value="system">System</SelectItem>
          </SelectContent>
        </Select>
      </CardContent>
    </Card>
  );
}
