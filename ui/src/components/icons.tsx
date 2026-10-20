// The one icon set used across Modwen: Lucide (ISC), bundled with the app (no requests, works offline).
// Same size and stroke everywhere; arrows that point "forward" flip in the Arabic (RTL) layout.
import type { LucideIcon, LucideProps } from "lucide-react";
import {
  ArrowDown, ArrowLeft, ArrowUpRight, AudioLines, Bell, Bookmark, Captions, Check, ChevronDown, CircleCheck, CircleHelp, CircleX,
  Copy, Cpu, Download, FileDown, Info, Languages, LoaderCircle, Lock, Menu, Mic, Monitor, Moon, Pause, Pencil,
  PictureInPicture2, Play, Plus, RotateCw, Search, Settings, ShieldCheck, Square, Sun, Trash2, TriangleAlert, Upload,
  UserCheck, UserPlus, UsersRound, Volume2, Wifi, X,
} from "lucide-react";

function make(Icon: LucideIcon, flip = false) {
  const Wrapped = ({ size = 16, className = "", ...rest }: LucideProps) => (
    <Icon size={size} strokeWidth={1.8} aria-hidden="true" focusable="false"
      className={`icon ${flip ? "flip-rtl" : ""} ${className}`.trim()} {...rest} />
  );
  return Wrapped;
}

export const I = {
  record: make(Mic),
  importFile: make(Upload),
  regenerate: make(RotateCw),
  chevron: make(ChevronDown),
  copy: make(Copy),
  exportFile: make(FileDown),
  speakers: make(UsersRound),
  people: make(UsersRound),
  settings: make(Settings),
  lock: make(Lock),
  search: make(Search),
  menu: make(Menu),
  warn: make(TriangleAlert),
  check: make(Check),
  close: make(X),
  mark: make(Bookmark),
  pause: make(Pause),
  play: make(Play),
  stop: make(Square),
  popOut: make(PictureInPicture2),
  live: make(Captions),
  trash: make(Trash2),
  voice: make(AudioLines),
  bell: make(Bell),
  download: make(Download),
  down: make(ArrowDown),
  jump: make(ArrowUpRight, true),
  back: make(ArrowLeft, true),
  edit: make(Pencil),
  addPerson: make(UserPlus),
  confirm: make(UserCheck),
  privacy: make(ShieldCheck),
  info: make(Info),
  ok: make(CircleCheck),
  fail: make(CircleX),
  unsure: make(CircleHelp),
  spin: make(LoaderCircle),
  language: make(Languages),
  sun: make(Sun),
  moon: make(Moon),
  system: make(Monitor),
  chip: make(Cpu),
  network: make(Wifi),
  callAudio: make(Volume2),
  plus: make(Plus),
};
