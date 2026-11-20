; Inno Setup 7 script for the Latentroom installer.
; Build the exe first (PyInstaller, dist\Latentroom\), then: ISCC.exe packaging\latentroom.iss
; scripts\build.ps1 does both. Output: dist\Latentroom-Setup-<version>.exe
;
; Per-user install, no admin rights: %LOCALAPPDATA%\Programs\Latentroom.
; The version comes from Latentroom.exe's version resource, which the PyInstaller spec takes
; from latentroom/__init__.py, so there is one place to bump it.
; Silent uninstall for scripts: unins000.exe /VERYSILENT [/DELETEMODELS=1]

#define AppExe "Latentroom.exe"
#define Bundle AddBackslash(SourcePath) + "..\dist\Latentroom"
#define AppVersion GetStringFileInfo(Bundle + "\" + AppExe, "ProductVersion")
#if AppVersion == ""
  #error dist\Latentroom\Latentroom.exe is missing or has no version: build the exe first
#endif

[Setup]
AppId={{C0349225-2BE4-45F1-BA50-194AA43E02C6}
AppName=Latentroom
AppVersion={#AppVersion}
AppVerName=Latentroom {#AppVersion}
AppPublisher=smb
AppPublisherURL=https://github.com/SamueleBassoli/latentroom
AppSupportURL=https://github.com/SamueleBassoli/latentroom/issues
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
DefaultDirName={userpf}\Latentroom
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=Latentroom-Setup-{#AppVersion}
SetupIconFile=..\latentroom\web\static\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName=Latentroom
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; An update must not mix old and new program files.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#Bundle}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#AddBackslash(SourcePath)}..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Latentroom"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\Latentroom"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,Latentroom}"; Flags: nowait postinstall skipifsilent

[Code]
const
  NL = #13#10; { a line may not start with '#': the preprocessor would read it as a directive }
  WebView2Key = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2Url = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'; { Evergreen Bootstrapper }
  OwnFiles = 'latentroom-files.txt'; { = paths.OWN_FILES }
  CSIDL_MYPICTURES = $0027; { ShlObj.h: the known Pictures folder }

{ Microsoft's documented check (same as app.webview2_installed): "pv" set and not 0.0.0.0,
  machine-wide (32-bit registry view) or per-user. }
function HasWebView2(const Root: Integer): Boolean;
var
  V: String;
begin
  Result := RegQueryStringValue(Root, WebView2Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

function InitializeSetup: Boolean;
var
  Code: Integer;
begin
  Result := True; { install anyway: Latentroom shows the same advice when it starts }
  if not (HasWebView2(HKLM32) or HasWebView2(HKCU)) then
    if SuppressibleMsgBox('Latentroom needs Microsoft Edge WebView2 to show its window, and it is ' +
        'missing on this PC.' + #13#10#13#10 + 'It is free and comes from Microsoft. ' +
        'Download it now? Latentroom will be installed either way; run the downloaded file ' +
        'before you open Latentroom.', mbConfirmation, MB_YESNO, IDNO) = IDYES then
      ShellExec('open', WebView2Url, '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

{ A path the way Python's Path() prints it: backslashes, no trailing one. }
function NormPath(P: String): String;
begin
  StringChangeEx(P, '/', '\', True);
  Result := RemoveBackslashUnlessRoot(P);
end;

{ The app's own folder: settings.json, logs. Same rule as paths.app_dir(). }
function AppDataDir: String;
begin
  Result := GetEnv('LATENTROOM_HOME');
  if Result = '' then
    Result := GetEnv('LOCALAPPDATA') + '\Latentroom';
  if Result = '\Latentroom' then
    Result := ExpandConstant('{localappdata}\Latentroom');
  Result := NormPath(Result);
end;

{ The Pictures folder where Windows keeps it, also when it was moved (OneDrive, another drive);
  ~\Pictures when Windows cannot say. Same rule as paths.pictures_dir(). False: never create it. }
function PicturesDir: String;
begin
  Result := GetShellFolderByCSIDL(CSIDL_MYPICTURES, False);
  if Result = '' then
    Result := GetEnv('USERPROFILE') + '\Pictures';
  Result := NormPath(Result);
end;

{ Same mutex name as app.claim_instance(): sha256 of the lower-cased app folder, 16 hex chars. }
function LatentroomRunning: Boolean;
begin
  Result := CheckForMutexes('Local\Latentroom-' +
    Copy(GetSHA256OfString(Utf8Encode(AnsiLowerCase(AppDataDir))), 1, 16));
end;

function IsAbsolute(const P: String): Boolean;
begin
  Result := ((Length(P) >= 3) and (P[2] = ':') and ((P[3] = '\') or (P[3] = '/')))
    or (Copy(P, 1, 2) = '\\');
end;

{ The string value of a top-level key in settings.json ('' when missing or not a string).
  Python writes it with json.dump defaults: ASCII only, \uXXXX for anything else. }
function JsonString(const Json, Key: String): String;
var
  I, N: Integer;
  C: Char;
begin
  Result := '';
  I := Pos('"' + Key + '"', Json);
  if I = 0 then
    Exit;
  I := I + Length(Key) + 2;
  N := Length(Json);
  while (I <= N) and (Json[I] <> '"') do
  begin
    if (Json[I] <> ':') and (Json[I] <> ' ') and (Json[I] <> #9) and (Json[I] <> #10)
        and (Json[I] <> #13) then
      Exit; { not a string value }
    I := I + 1;
  end;
  I := I + 1;
  while I <= N do
  begin
    C := Json[I];
    if C = '"' then
      Exit; { end of the value }
    if C = '\' then
    begin
      I := I + 1;
      if I > N then
        Break;
      C := Json[I];
      if C = 'u' then
      begin
        C := Chr(StrToIntDef('$' + Copy(Json, I + 1, 4), Ord('?')));
        I := I + 4;
      end
      else if C = 'n' then C := #10
      else if C = 't' then C := #9
      else if C = 'r' then C := #13
      else if C = 'b' then C := #8
      else if C = 'f' then C := #12; { \\ \" \/ stand for themselves }
    end;
    Result := Result + C;
    I := I + 1;
  end;
  Result := ''; { unterminated string: treat as missing }
end;

function InitializeUninstall: Boolean;
begin
  Result := not LatentroomRunning;
  if not Result then
    SuppressibleMsgBox('Latentroom is still open. Close Latentroom, then uninstall it again.',
      mbError, MB_OK, IDOK);
end;

{ The data folder may be shared with another app (D:\AI with ComfyUI's models\), so
  never delete a whole models\ or engine\ folder. Delete only what the app listed as its own in
  OwnFiles (install.write_own_files): one name directly in models\ or engine\ (a trailing \ is
  a folder Latentroom unzipped), or downloads.json. Any other line is skipped. }
function OwnPath(const DataDir, Line: String): String;
var
  Name: String;
begin
  Result := '';
  if Line = 'downloads.json' then
  begin
    Result := AddBackslash(DataDir) + Line;
    Exit;
  end;
  if (CompareText(Copy(Line, 1, 7), 'models\') <> 0) and (CompareText(Copy(Line, 1, 7), 'engine\') <> 0) then
    Exit;
  Name := Copy(Line, 8, Length(Line));
  if (Name <> '') and (Name[Length(Name)] = '\') then
    Name := Copy(Name, 1, Length(Name) - 1);
  if (Name = '') or (Name = '.') or (Pos('..', Name) > 0) or (Pos('\', Name) > 0) or (Pos('/', Name) > 0)
      or (Pos(':', Name) > 0) then
    Exit;
  Result := AddBackslash(DataDir) + Copy(Line, 1, 7) + Name;
end;

procedure DeleteOwnFiles(const DataDir: String);
var
  Lines: TArrayOfString;
  I: Integer;
  P: String;
begin
  if not LoadStringsFromFile(AddBackslash(DataDir) + OwnFiles, Lines) then
    Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    P := OwnPath(DataDir, Trim(Lines[I]));
    if P = '' then
      Continue;
    if DirExists(P) then
      DelTree(P, True, True, True)
    else
      DeleteFile(P);
  end;
  DeleteFile(AddBackslash(DataDir) + OwnFiles);
  RemoveDir(AddBackslash(DataDir) + 'models'); { only when nothing else is left in it }
  RemoveDir(AddBackslash(DataDir) + 'engine');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Raw: AnsiString;
  Settings, AppDir, DataDir, OutDir, Kept: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  AppDir := AppDataDir;
  if LoadStringFromFile(AppDir + '\settings.json', Raw) then
    Settings := String(Raw)
  else
    Settings := '';
  { A relative or empty path is never trusted for deleting: fall back to the app folder. }
  DataDir := JsonString(Settings, 'data_dir');
  if IsAbsolute(DataDir) then
    DataDir := NormPath(DataDir)
  else
    DataDir := AppDir;
  OutDir := JsonString(Settings, 'output_dir');
  if IsAbsolute(OutDir) then
    OutDir := NormPath(OutDir)
  else
    if GetEnv('LATENTROOM_HOME') <> '' then
      OutDir := AppDir + '\output'
    else
      OutDir := PicturesDir + '\Latentroom'; { same rule as paths.default_output_dir() }

  { Question 1: the big downloads. Default No. Only the files the app listed as its own (B2):
    no list (the app never ran in this folder), no question and nothing deleted. }
  if FileExists(AddBackslash(DataDir) + OwnFiles) then
    if (ExpandConstant('{param:DELETEMODELS|0}') = '1') or (SuppressibleMsgBox(
        'Also delete the models and the image engine Latentroom downloaded?' + #13#10#13#10 +
        'They use several GB of disk space, in:' + #13#10 +
        AddBackslash(DataDir) + 'models' + #13#10 + AddBackslash(DataDir) + 'engine' + #13#10 +
        NL + 'Choose No if you may install Latentroom again: you will not have to ' +
        'download them again. Only files Latentroom downloaded are deleted: other files in ' +
        'these folders, your pictures and your own LoRA files are never touched.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES) then
      DeleteOwnFiles(DataDir);

  { Message 2: pictures are never deleted; say where they are. }
  Kept := AppDir;
  if CompareText(DataDir, AppDir) <> 0 then
    Kept := Kept + #13#10 + DataDir;
  SuppressibleMsgBox('Latentroom has been removed.' + #13#10#13#10 +
    'Your pictures were not touched. They are in:' + #13#10 + OutDir + #13#10#13#10 +
    'Your settings, Library and own LoRA files were kept too, in:' + #13#10 + Kept + #13#10 +
    'Delete these folders yourself if you no longer need them.',
    mbInformation, MB_OK, IDOK);
end;
