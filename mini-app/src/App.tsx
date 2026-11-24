import { AppRoot, ConfigProvider, Panel, View } from '@vkontakte/vkui';
import { AccessScreen } from './components/AccessScreen';
import { Chat } from './components/Chat';
import { useSession } from './useSession';

export default function App() {
  const { status, session, error, retry, setDialog, expire } = useSession();
  return (
    <ConfigProvider>
      <AppRoot>
        <View activePanel="main">
          <Panel id="main" style={{ minHeight: '100vh' }}>
            {status === 'ready' && session
              ? <Chat key={session.dialog_id} session={session} onDialog={setDialog} onExpired={expire} />
              : <AccessScreen status={status} error={error || undefined} onRetry={retry} />}
          </Panel>
        </View>
      </AppRoot>
    </ConfigProvider>
  );
}
