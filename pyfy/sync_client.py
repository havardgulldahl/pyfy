try:
    import json
except:
    import ujson
import logging
import datetime
from urllib import parse
from urllib3.util import Retry

from requests import Request, Session, Response
from requests.exceptions import HTTPError, Timeout
from requests.adapters import HTTPAdapter
from cachecontrol import CacheControlAdapter

from .creds import (
    ClientCreds,
    _set_empty_user_creds_if_none
)
from .excs import ApiError, AuthError
from .utils import (
    _safe_getitem,
    _locale_injectable,
    _nullable_response,
    _prep_request,
    _set_and_get_me_attr_sync
)
from .base_client import (
    BaseClient,
    TOKEN_EXPIRED_MSG,
    BASE_URI,
)


logger = logging.getLogger(__name__)


class Spotify(BaseClient):
    def __init__(self, access_token=None, client_creds=ClientCreds(), user_creds=None, ensure_user_auth=False, proxies={}, timeout=7,
                max_retries=10, enforce_state_check=True, backoff_factor=0.1, default_to_locale=True, cache=True, populate_user_creds=True):
        '''
        Parameters:
            client_creds: A client credentials model
            user_creds: A user credentials model
            ensure_user_auth: Whether or not to fail if user_creds provided where invalid and not refresheable
            proxies: socks or http proxies # http://docs.python-requests.org/en/master/user/advanced/#proxies & http://docs.python-requests.org/en/master/user/advanced/#socks
            timeout: Seconds before request raises a timeout error
            max_retries: Max retries before a request fails
            enforce_state_check: Check for a CSRF-token-like string. Helps verifying the identity of a callback sender thus avoiding CSRF attacks. Optional
            backoff_factor: Factor by which requests delays the next request when encountring a 429 too-many-requests error
            default_to_locale: Will pass methods decorated with @locale_injecteable the user's locale if available. (must have populate_user_creds)
            cache: Whether or not to cache HTTP requests for the user
            populate_user_creds: Sets user_creds info from Spotify to client's user_creds object. e.g. country. WILL OVERWRITE DATA SET TO USER CREDS IF SET TO TRUE
        '''
        self._is_async = False  # Client is synchronous
        super().__init__(access_token, client_creds, user_creds, ensure_user_auth, proxies,
            timeout, max_retries, enforce_state_check, backoff_factor, default_to_locale, cache, populate_user_creds)
        if populate_user_creds and self.user_creds:
            self.populate_user_creds()

    def populate_user_creds(self):
        me = self.me
        if me:
            self._populate_user_creds(me)

    def _create_session(self, max_retries, proxies, backoff_factor, cache):
        sess = Session()
        # Retry only on idempotent methods and only when too many requests
        retries = Retry(total=max_retries, backoff_factor=backoff_factor, status_forcelist=[429], method_whitelist=['GET', 'UPDATE', 'DELETE'])
        retries_adapter = HTTPAdapter(max_retries=retries)
        if cache:
            cache_adapter = CacheControlAdapter(cache_etags=True)
        sess.mount('http://', retries_adapter)
        sess.mount('http://', cache_adapter)
        sess.proxies.update(proxies)  
        return sess

    @_prep_request
    def _check_authorization(self, **kwargs):
        '''
        Checks whether the credentials provided are valid or not by making and api call that requires no scope but still requires authorization
        '''
        try:
            self._send_authorized_request(kwargs['r'])
        except AuthError as e:
            raise e

    def _send_authorized_request(self, r):
        if getattr(self._caller, 'access_is_expired', None) is True:  # True if expired and None if there's no expiry set
            self._refresh_token()
        r.headers.update(self._access_authorization_header)
        return self._send_request(r)

    def _send_request(self, r):
        prepped = r.prepare()
        try:
            res = self._session.send(prepped, timeout=self.timeout)
            res.raise_for_status()
        except Timeout as e:
            raise ApiError('Request timed out.\nTry increasing the client\'s timeout period', http_response=Response(), http_request=r, e=e)
        except HTTPError as e:
            if res.status_code == 401:
                if res.json().get('error', None).get('message', None) == TOKEN_EXPIRED_MSG:
                    old_auth_header = r.headers['Authorization']
                    self._refresh_token()  # Should either raise an error or refresh the token
                    new_auth_header = self._access_authorization_header
                    assert new_auth_header != old_auth_header  # Assert header is changed to avoid infinite loops
                    r.headers.update(new_auth_header)
                    return self._send_request(r)
                else:
                    msg = res.json().get('error_description') or res.json()
                    raise AuthError(msg=msg, http_response=res, http_request=r, e=e)
            else:
                msg = _safe_getitem(res.json(), 'error', 'message') or _safe_getitem(res.json(), 'error_description')
                raise ApiError(msg=msg, http_response=res, http_request=r, e=e)
        else:
            return res

    def authorize_client_creds(self, client_creds=None):
        ''' https://developer.spotify.com/documentation/general/guides/authorization-guide/ 
            Authorize with client credentials oauth flow i.e. Only with client secret and client id.
            This will give you limited functionality '''

        r = self._prep_authorize_client_creds(client_creds)
        try:
            res = self._send_request(r)
        except ApiError as e:
            raise AuthError(msg='Failed to authenticate with client credentials', http_response=e.http_response, http_request=r, e=e)
        else:
            new_creds_json = res.json()
            new_creds_model = self._client_json_to_object(new_creds_json)
            self._update_client_creds_with(new_creds_model)
            self._caller = self.client_creds
            self._check_authorization()

    @property
    def is_active(self):
        '''
        Checks if user_creds or client_creds are valid (depending on who was last set)
        '''
        if self._caller is None:
            return False
        try:
            self._check_authorization()
        except AuthError:
            return False
        else:
            return True

    def _refresh_token(self):
        if self._caller is self.user_creds:
            return self._refresh_user_token()
        elif self._caller is self.client_creds:
            return self.authorize_client_creds()
        else:
            raise AuthError('No caller to refresh token for')

    def _refresh_user_token(self):
        r = self._prep_refresh_user_token()
        res = self._send_request(r).json()
        new_creds_obj = self._user_json_to_object(res)
        self._update_user_creds_with(new_creds_obj)

    @_set_empty_user_creds_if_none
    def build_user_creds(self, grant, state=None, enforce_state_check=None, set_user_creds=True):
        '''
        Second part of OAuth authorization code flow, Raises an AuthError if unauthorized
        Parameters:
            - grant: Code returned to user after authorizing your application
            - state: State returned from oauth callback
            - enforce_state_check: Check for a CSRF-token-like string. Helps verifying the identity of a callback sender thus avoiding CSRF attacks. Optional
            - set_user_creds: Whether or not to set the user created to the client as the current active user
        '''
        if enforce_state_check is not None:
            self.enforce_state_check = enforce_state_check
        self._check_for_state(grant, state, set_user_creds)

        # Get user creds
        user_creds_json = self._request_user_creds(grant).json()
        user_creds_model = self._user_json_to_object(user_creds_json)

        # Set user creds
        if set_user_creds:
            self.user_creds = user_creds_model
        return user_creds_model

    @_prep_request
    def _request_user_creds(self, grant, **kwargs):
        return self._send_request(kwargs['r'])

    ####################################################################### RESOURCES ############################################################################

##### Playback
    @_prep_request
    def devices(self, **kwargs):
        ''' Lists user's devices '''
        return self._send_authorized_request(kwargs['r']).json()


    @_nullable_response
    @_prep_request
    def play(self, resource_id=None, resource_type='track', device_id=None, offset_position=None, position_ms=None, **kwargs):
        ''' Available types: 'track', 'artist', 'playlist', 'podcast', 'user' not sure if there's more'''
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def pause(self, device_id=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()


    @_prep_request
    @_locale_injectable('market')
    def currently_playing(self, market=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()


    @_prep_request
    @_locale_injectable('market')
    def currently_playing_info(self, market=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()


    @_prep_request
    def recently_played_tracks(self, limit=None, after=None, before=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()


    @_nullable_response
    @_prep_request
    def next(self, device_id=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()


    @_nullable_response
    @_prep_request
    def previous(self, device_id=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()


    @_nullable_response
    @_prep_request
    def repeat(self, state='context', device_id=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()


    @_nullable_response
    @_prep_request
    def seek(self, position_ms, device_id=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()


    @_nullable_response
    @_prep_request
    def shuffle(self, state=True, device_id=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def playback_transfer(self, device_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def volume(self, volume_percent, device_id=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

##### Playlists

    @_prep_request
    @_locale_injectable('market')
    def playlist(self, playlist_id, market=None, fields=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def user_playlists(self, user_id=None, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def _user_playlists(self, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    def follows_playlist(self, playlist_id, user_ids=None, **kwargs):
        if user_ids is None:
            user_ids = _set_and_get_me_attr_sync(self, 'id')
        r = self._prep_follows_playlist(playlist_id, user_ids)
        return self._send_authorized_request(r).json()

    @_nullable_response
    def create_playlist(self, name, description=None, public=False, collaborative=False, **kwargs):
        user_id = _set_and_get_me_attr_sync(self, 'id')
        r = self._prep_create_playlist(name, user_id, description, public, collaborative)
        return self._send_authorized_request(r).json()

    @_nullable_response
    @_prep_request
    def follow_playlist(self, playlist_id, public=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def update_playlist(self, playlist_id, name=None, description=None, public=None, collaborative=False, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def unfollow_playlist(self, playlist_id, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def delete_playlist(self, playlist_id, **kwargs):
        ''' an alias to unfollow_playlist''' 
        return self._send_authorized_request(kwargs['r']).json()



##### Playlist Contents


    @_prep_request
    @_locale_injectable('market')
    def playlist_tracks(self, playlist_id, market=None, fields=None, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def add_playlist_tracks(self, playlist_id, track_ids, position=None, **kwargs):
        ''' track_ids can be a list of track ids or a string of one track_id'''
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def reorder_playlist_track(self, playlist_id, range_start=None, range_length=None, insert_before=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def delete_playlist_tracks(self, playlist_id, track_uris, **kwargs):
        ''' 
        track_uris types supported:
        1) 'track_uri'
        2) ['track_uri', 'track_uri', 'track_uri']
        3) [
            {
                'uri': track_uri,
                'positions': [
                    position1, position2
                ]
            },
            {
                'uri': track_uri,
                'positions': position1
            },
            track_uri
        ]
        '''
        # https://developer.spotify.com/console/delete-playlist-tracks/
        return self._send_authorized_request(kwargs['r']).json()

##### Tracks

    @_prep_request
    @_locale_injectable('market')
    def user_tracks(self, market=None, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    @_locale_injectable('market')
    def tracks(self, track_ids, market=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def _track(self, track_id, market=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def owns_tracks(self, track_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def save_tracks(self, track_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def delete_tracks(self, track_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

##### Artists

    @_prep_request
    def artists(self, artist_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def _artist(self, artist_id, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def followed_artists(self, after=None, limit=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def follows_artists(self, artist_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def follow_artists(self, artist_ids, **kwargs):       
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def unfollow_artists(self, artist_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def artist_related_artists(self, artist_id, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    @_locale_injectable('country')
    def artist_top_tracks(self, artist_id, country=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

##### Albums

    @_prep_request
    @_locale_injectable('market')
    def albums(self, album_ids, market=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def _album(self, album_id, market=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def user_albums(self, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def owns_albums(self, album_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def save_albums(self, album_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def delete_albums(self, album_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

##### Users

    @property
    @_prep_request
    def me(self, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @property
    def is_premium(self):
        if self.me.get('type') == 'premium':
            return True
        return False

    @_prep_request
    def user_profile(self, user_id, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def follows_users(self, user_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def follow_users(self, user_ids, **kwargs):       
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def unfollow_users(self, user_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

##### Others

    @_prep_request
    @_locale_injectable('market')
    def album_tracks(self, album_id, market=None, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    @_locale_injectable('market')
    def artist_albums(self, artist_id, include_groups=None, market=None, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def user_top_tracks(self, time_range=None, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def user_top_artists(self, time_range=None, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_nullable_response
    @_prep_request
    def next_page(self, response=None, url=None, **kwargs):
        '''
        You can provide either a response dict or a url
        Providing a URL will be slightly faster as Pyfy will not have to search for the key in the response dict
        '''
        if kwargs['r'] is not None:
            return self._send_authorized_request(kwargs['r']).json()
        return {}

    @_nullable_response
    @_prep_request
    def previous_page(self, response=None, url=None, **kwargs):
        '''
        You can provide either a response dict or a url
        Providing a URL will be slightly faster as Pyfy will not have to search for the key in the response dict
        '''
        if kwargs['r'] is not None:
            return self._send_authorized_request(kwargs['r']).json()
        return {}

##### Personalization & Explore

    @_prep_request
    @_locale_injectable('country', support_from_token=False)
    def category(self, category_id, country=None, locale=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    @_locale_injectable('country', support_from_token=False)
    def categories(self, country=None, locale=None, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    @_locale_injectable('country', support_from_token=False)
    def category_playlist(self, category_id, country=None, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def available_genre_seeds(self, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    @_locale_injectable('country', support_from_token=False)
    def featured_playlists(self, country=None, locale=None, timestamp=None, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    @_locale_injectable('country', support_from_token=False)
    def new_releases(self, country=None, limit=None, offset=None, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    @_locale_injectable('market')
    def search(self, q, types='track', market=None, limit=None, offset=None, **kwargs):
        ''' 'track' or ['track'] or 'artist' or ['track','artist'] '''
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def track_audio_analysis(self, track_id, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def _track_audio_features(self, track_id, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    def tracks_audio_features(self, track_ids, **kwargs):
        return self._send_authorized_request(kwargs['r']).json()

    @_prep_request
    @_locale_injectable('market', support_from_token=False)
    def recommendations(
        self,
        limit=None,
        market=None,
        seed_artists=None,
        seed_genres=None,
        seed_tracks=None,
        min_acousticness=None,
        max_acousticness=None,
        target_acousticness=None,
        min_danceability=None,
        max_danceability=None,
        target_danceability=None,
        min_duration_ms=None,
        max_duration_ms=None,
        target_duration_ms=None,
        min_energy=None,
        max_energy=None,
        target_energy=None,
        min_instrumentalness=None,
        max_instrumentalness=None,
        target_instrumentalness=None,
        min_key=None,
        max_key=None,
        target_key=None,
        min_liveness=None,
        max_liveness=None,
        target_liveness=None,
        min_loudness=None,
        max_loudness=None,
        target_loudness=None,
        min_mode=None,
        max_mode=None,
        target_mode=None,
        min_popularity=None,
        max_popularity=None,
        target_popularity=None,
        min_speechiness=None,
        max_speechiness=None,
        target_speechiness=None,
        min_tempo=None,
        max_tempo=None,
        target_tempo=None,
        min_time_signature=None,
        max_time_signature=None,
        target_time_signature=None,
        min_valence=None,
        max_valence=None,
        target_valence=None,
        **kwargs
    ):
        ''' https://developer.spotify.com/documentation/web-api/reference/browse/get-recommendations/ '''
        url = BASE_URI + '/recommendations'
        params = dict(
            limit=limit,
            market=market,
            seed_artists=seed_artists,
            seed_genres=seed_genres,
            seed_tracks=seed_tracks,
            min_acousticness=min_acousticness,
            max_acousticness=max_acousticness,
            target_acousticness=target_acousticness,
            min_danceability=min_danceability,
            max_danceability=max_danceability,
            target_danceability=target_danceability,
            min_duration_ms=min_duration_ms,
            max_duration_ms=max_duration_ms,
            target_duration_ms=target_duration_ms,
            min_energy=min_energy,
            max_energy=max_energy,
            target_energy=target_energy,
            min_instrumentalness=min_instrumentalness,
            max_instrumentalness=max_instrumentalness,
            target_instrumentalness=target_instrumentalness,
            min_key=min_key,
            max_key=max_key,
            target_key=target_key,
            min_liveness=min_liveness,
            max_liveness=max_liveness,
            target_liveness=target_liveness,
            min_loudness=min_loudness,
            max_loudness=max_loudness,
            target_loudness=target_loudness,
            min_mode=min_mode,
            max_mode=max_mode,
            target_mode=target_mode,
            min_popularity=min_popularity,
            max_popularity=max_popularity,
            target_popularity=target_popularity,
            min_speechiness=min_speechiness,
            max_speechiness=max_speechiness,
            target_speechiness=target_speechiness,
            min_tempo=min_tempo,
            max_tempo=max_tempo,
            target_tempo=target_tempo,
            min_time_signature=min_time_signature,
            max_time_signature=max_time_signature,
            target_time_signature=target_time_signature,
            min_valence=min_valence,
            max_valence=max_valence,
            target_valence=target_valence
        )
        return self._send_authorized_request(kwargs['r']).json()