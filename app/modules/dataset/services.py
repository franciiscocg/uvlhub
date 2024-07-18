import logging
import os
import hashlib
import shutil
import tempfile
from typing import Optional
import uuid
from zipfile import ZipFile

from flask import request

from app.modules.auth.models import User
from app.modules.auth.services import AuthenticationService
from app.modules.dataset.forms import AuthorForm, DataSetForm, FeatureModelForm
from app.modules.dataset.models import DSDownloadRecord, DSViewRecord, DataSet, DSMetaData
from app.modules.dataset.repositories import (
    AuthorRepository,
    DOIMappingRepository,
    DSDownloadRecordRepository,
    DSMetaDataRepository,
    DSViewRecordRepository,
    DataSetRepository
)
from app.modules.featuremodel.repositories import FMMetaDataRepository, FeatureModelRepository
from app.modules.hubfile.repositories import (
    HubfileDownloadRecordRepository,
    HubfileRepository,
    HubfileViewRecordRepository
)
from core.services.BaseService import BaseService

logger = logging.getLogger(__name__)


def calculate_checksum_and_size(file_path):
    file_size = os.path.getsize(file_path)
    with open(file_path, "rb") as file:
        content = file.read()
        hash_md5 = hashlib.md5(content).hexdigest()
        return hash_md5, file_size


class DataSetService(BaseService):
    def __init__(self):
        super().__init__(DataSetRepository())
        self.feature_model_repository = FeatureModelRepository()
        self.author_repository = AuthorRepository()
        self.dsmetadata_repository = DSMetaDataRepository()
        self.fmmetadata_repository = FMMetaDataRepository()
        self.dsdownloadrecord_repository = DSDownloadRecordRepository()
        self.hubfiledownloadrecord_repository = HubfileDownloadRecordRepository()
        self.hubfilerepository = HubfileRepository()
        self.dsviewrecord_repostory = DSViewRecordRepository()
        self.hubfileviewrecord_repository = HubfileViewRecordRepository()

    def move_feature_models(self, dataset: DataSet):
        current_user = AuthenticationService().get_authenticated_user()
        source_dir = current_user.temp_folder()

        working_dir = os.getenv("WORKING_DIR", "")
        dest_dir = os.path.join(working_dir, "uploads", f"user_{current_user.id}", f"dataset_{dataset.id}")

        os.makedirs(dest_dir, exist_ok=True)

        for feature_model in dataset.feature_models:
            uvl_filename = feature_model.fm_meta_data.uvl_filename
            shutil.move(os.path.join(source_dir, uvl_filename), dest_dir)

    def get_synchronized(self, current_user_id: int) -> DataSet:
        return self.repository.get_synchronized(current_user_id)

    def get_unsynchronized(self, current_user_id: int) -> DataSet:
        return self.repository.get_unsynchronized(current_user_id)

    def get_unsynchronized_dataset(self, current_user_id: int, dataset_id: int) -> DataSet:
        return self.repository.get_unsynchronized_dataset(current_user_id, dataset_id)

    def latest_synchronized(self):
        return self.repository.latest_synchronized()

    def count_synchronized_datasets(self):
        return self.repository.count_synchronized_datasets()

    def count_feature_models(self):
        return self.feature_model_service.count_feature_models()

    def count_authors(self) -> int:
        return self.author_repository.count()

    def count_dsmetadata(self) -> int:
        return self.dsmetadata_repository.count()

    def total_dataset_downloads(self) -> int:
        return self.dsdownloadrecord_repository.total_dataset_downloads()

    def total_dataset_views(self) -> int:
        return self.dsviewrecord_repostory.total_dataset_views()

    def update_from_form(self, form: DataSetForm, current_user: User, dataset: DataSet) -> DataSet:
        main_author = {
            "name": f"{current_user.profile.surname}, {current_user.profile.name}",
            "affiliation": current_user.profile.affiliation,
            "orcid": current_user.profile.orcid,
        }
        try:

            # Update dataset metadata
            logger.info(f"Updating dsmetadata...: {form.get_dsmetadata()}")
            dsmetadata = self.dsmetadata_repository.update(id=dataset.ds_meta_data.id,
                                                           **form.get_dsmetadata())

            # Update authors
            dsmetadata_info = form.get_dsmetadata()
            is_anonymous = dsmetadata_info.get('dataset_anonymous', False)

            self.author_repository.delete_by_column(column_name="ds_meta_data_id",
                                                    value=dataset.ds_meta_data.id)

            if is_anonymous:
                author_list = form.get_anonymous_authors()
            else:
                other_authors = form.get_authors()
                if other_authors:
                    author_list = other_authors
                else:
                    author_list = [main_author]

            for author_data in author_list:
                author = self.author_repository.create(commit=False, ds_meta_data_id=dsmetadata.id, **author_data)
                dsmetadata.authors.append(author)

            #   Save updated data in local
            self.repository.session.commit()

        except Exception as exc:
            logger.info(f"Exception updating dataset from form...: {exc}")
            self.repository.session.rollback()
            raise exc

        return self.get_by_id(dataset.id)

    def create_from_form(self, form: DataSetForm, current_user: User) -> DataSet:

        dataset = None

        main_author = {
            "name": f"{current_user.profile.surname}, {current_user.profile.name}",
            "affiliation": current_user.profile.affiliation,
            "orcid": current_user.profile.orcid,
        }
        try:
            logger.info(f"Creating dsmetadata...: {form.get_dsmetadata()}")
            dsmetadata = self.dsmetadata_repository.create(**form.get_dsmetadata())

            dsmetadata_info = form.get_dsmetadata()
            is_anonymous = dsmetadata_info.get('dataset_anonymous', False)

            if is_anonymous:
                author_list = form.get_anonymous_authors()
            else:
                other_authors = form.get_authors()
                if other_authors:
                    author_list = other_authors
                else:
                    author_list = [main_author]

            for author_data in author_list:
                author = self.author_repository.create(commit=False, ds_meta_data_id=dsmetadata.id, **author_data)
                dsmetadata.authors.append(author)

            dataset = self.create(commit=False, user_id=current_user.id, ds_meta_data_id=dsmetadata.id)

            for feature_model in form.feature_models:
                uvl_filename = feature_model.uvl_filename.data
                fmmetadata = self.fmmetadata_repository.create(commit=False, **feature_model.get_fmmetadata())
                for author_data in feature_model.get_authors():
                    author = self.author_repository.create(commit=False, fm_meta_data_id=fmmetadata.id, **author_data)
                    fmmetadata.authors.append(author)

                fm = self.feature_model_repository.create(
                    commit=False, data_set_id=dataset.id, fm_meta_data_id=fmmetadata.id
                )

                # associated files in feature model
                file_path = os.path.join(current_user.temp_folder(), uvl_filename)
                checksum, size = calculate_checksum_and_size(file_path)

                file = self.hubfilerepository.create(
                    commit=False, name=uvl_filename, checksum=checksum, size=size, feature_model_id=fm.id
                )
                fm.files.append(file)
            self.repository.session.commit()
        except Exception as exc:
            logger.info(f"Exception creating dataset from form...: {exc}")
            self.repository.session.rollback()
            raise exc

        return dataset

    def populate_form_from_dataset(self, form: DataSetForm, dataset: DataSet):
        ds_meta_data = dataset.ds_meta_data

        form.title.data = ds_meta_data.title
        form.desc.data = ds_meta_data.description
        form.publication_type.data = ds_meta_data.publication_type.value
        form.publication_doi.data = ds_meta_data.publication_doi
        form.dataset_doi.data = ds_meta_data.dataset_doi
        form.tags.data = ds_meta_data.tags
        form.dataset_anonymous.data = ds_meta_data.dataset_anonymous

        # Populate authors
        form.authors.entries = []  # Clear existing entries
        for author in ds_meta_data.authors:
            author_form = AuthorForm()
            author_form.name.data = author.name
            author_form.affiliation.data = author.affiliation
            author_form.orcid.data = author.orcid
            form.authors.append_entry(author_form)

        # Populate feature models
        form.feature_models.entries = []  # Clear existing entries
        for fm in dataset.feature_models:
            fm_meta_data = fm.fm_meta_data
            fm_form = FeatureModelForm()
            fm_form.uvl_filename.data = fm_meta_data.uvl_filename
            fm_form.title.data = fm_meta_data.title
            fm_form.desc.data = fm_meta_data.description
            fm_form.publication_type.data = fm_meta_data.publication_type.value
            fm_form.publication_doi.data = fm_meta_data.publication_doi
            fm_form.tags.data = fm_meta_data.tags
            fm_form.version.data = fm_meta_data.uvl_version

            # Populate authors for feature model
            fm_form.authors.entries = []  # Clear existing entries
            for author in fm_meta_data.authors:
                author_form = AuthorForm()
                author_form.name.data = author.name
                author_form.affiliation.data = author.affiliation
                author_form.orcid.data = author.orcid
                fm_form.authors.append_entry(author_form)

            form.feature_models.append_entry(fm_form)

        return form

    def update_dsmetadata(self, id, **kwargs):
        return self.dsmetadata_repository.update(id, **kwargs)

    def get_uvlhub_doi(self, dataset: DataSet) -> str:
        domain = os.getenv('DOMAIN', 'localhost')
        return f'http://{domain}/doi/{dataset.ds_meta_data.dataset_doi}'

    def zip_dataset(self, dataset: DataSet) -> str:
        file_path = f"uploads/user_{dataset.user_id}/dataset_{dataset.id}/"
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, f"dataset_{dataset.id}.zip")

        with ZipFile(zip_path, "w") as zipf:
            for subdir, dirs, files in os.walk(file_path):
                for file in files:
                    full_path = os.path.join(subdir, file)

                    relative_path = os.path.relpath(full_path, file_path)

                    zipf.write(
                        full_path,
                        arcname=os.path.join(
                            os.path.basename(zip_path[:-4]), relative_path
                        ),
                    )

        return temp_dir


class AuthorService(BaseService):
    def __init__(self):
        super().__init__(AuthorRepository())


class DSDownloadRecordService(BaseService):
    def __init__(self):
        super().__init__(DSDownloadRecordRepository())

    def the_record_exists(self, dataset: DataSet, user_cookie: str):
        return self.repository.the_record_exists(dataset, user_cookie)

    def create_new_record(self, dataset: DataSet,  user_cookie: str) -> DSDownloadRecord:
        return self.repository.create_new_record(dataset, user_cookie)

    def create_cookie(self, dataset: DataSet) -> str:

        user_cookie = request.cookies.get("download_cookie")
        if not user_cookie:
            user_cookie = str(uuid.uuid4())

        existing_record = self.the_record_exists(dataset=dataset, user_cookie=user_cookie)

        if not existing_record:
            self.create_new_record(dataset=dataset, user_cookie=user_cookie)

        return user_cookie


class DSMetaDataService(BaseService):
    def __init__(self):
        super().__init__(DSMetaDataRepository())

    def update(self, id, **kwargs):
        return self.repository.update(id, **kwargs)

    def filter_by_doi(self, doi: str) -> Optional[DSMetaData]:
        return self.repository.filter_by_doi(doi)


class DSViewRecordService(BaseService):
    def __init__(self):
        super().__init__(DSViewRecordRepository())

    def the_record_exists(self, dataset: DataSet, user_cookie: str):
        return self.repository.the_record_exists(dataset, user_cookie)

    def create_new_record(self, dataset: DataSet,  user_cookie: str) -> DSViewRecord:
        return self.repository.create_new_record(dataset, user_cookie)

    def create_cookie(self, dataset: DataSet) -> str:

        user_cookie = request.cookies.get("view_cookie")
        if not user_cookie:
            user_cookie = str(uuid.uuid4())

        existing_record = self.the_record_exists(dataset=dataset, user_cookie=user_cookie)

        if not existing_record:
            self.create_new_record(dataset=dataset, user_cookie=user_cookie)

        return user_cookie


class DOIMappingService(BaseService):
    def __init__(self):
        super().__init__(DOIMappingRepository())

    def get_new_doi(self, old_doi: str) -> str:
        doi_mapping = self.repository.get_new_doi(old_doi)
        if doi_mapping:
            return doi_mapping.dataset_doi_new
        else:
            return None


class SizeService():

    def __init__(self):
        pass

    def get_human_readable_size(self, size: int) -> str:
        if size < 1024:
            return f'{size} bytes'
        elif size < 1024 ** 2:
            return f'{round(size / 1024, 2)} KB'
        elif size < 1024 ** 3:
            return f'{round(size / (1024 ** 2), 2)} MB'
        else:
            return f'{round(size / (1024 ** 3), 2)} GB'
